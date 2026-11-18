"""Page layout and components of the web interface, in the style of Sonarr/Radarr: header with search, a sidebar
of sections with their pages, a toolbar per page, settings forms with labels on the left and help text below, cards
for connections and dialogs to edit them. Dark by default, light when the browser prefers it. Standard library only.
"""
from __future__ import annotations

import html
from typing import Iterable, Optional


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


# Feather-style icons (stroke paths, 24x24)
ICONS = {
    "activity": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
    "library": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    "failed": '<path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5M12 18h.01"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "system": '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "trash": '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
    "play": '<path d="m6 4 14 8-14 8V4z"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "advanced": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "menu": '<path d="M3 6h18M3 12h18M3 18h18"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    "warning": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    "error": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/>',
    "ok": '<circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "disk": '<path d="M22 12H2M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1zM6 16h.01M10 16h.01"/>',
    "chip": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
}


# The mark, in the style of the *arr family (a filled circle with a segmented white ring), its own muted violet,
# and two arrows turning into each other for conversion.
LOGO = ('<span class="mark"><svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true">'
        '<defs><linearGradient id="mcg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8b78d0"/>'
        '<stop offset="1" stop-color="#5b4a9e"/></linearGradient></defs>'
        '<circle cx="16" cy="16" r="15" fill="url(#mcg)"/>'
        '<circle cx="16" cy="16" r="10.5" fill="none" stroke="#fff" stroke-opacity=".9" stroke-width="2.4" '
        'stroke-dasharray="13.5 3" transform="rotate(-50 16 16)" stroke-linecap="round"/>'
        '<g fill="none" stroke="#fff" stroke-opacity=".95" stroke-width="1.7" stroke-linecap="round" '
        'stroke-linejoin="round"><path d="M20.4 14.2a4.6 4.6 0 0 0-8.1-1.9"/><path d="M12 10.2v2.4h2.4"/>'
        '<path d="M11.6 17.8a4.6 4.6 0 0 0 8.1 1.9"/><path d="M20 21.8v-2.4h-2.4"/></g></svg></span>')


def icon(name: str, size: int = 18) -> str:
    return (f'<svg class="icon" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


# (section, label, icon, first page, [(page, label)])
SECTIONS = [
    ("activity", "Activity", "activity", "/queue", [("/queue", "Queue"), ("/history", "History"),
                                                    ("/events", "Events")]),
    ("library", "Library", "library", "/files", [("/files", "Files")]),
    ("failed", "Failed", "failed", "/errors", [("/errors", "Failed")]),
    ("settings", "Settings", "settings", "/settings", [("/settings/connect", "Connect"),
                                                       ("/settings/conversion", "Conversion"),
                                                       ("/settings/general", "General")]),
    ("system", "System", "system", "/system/status", [("/system/status", "Status"), ("/system/tasks", "Tasks"),
                                                      ("/hardware", "Hardware"), ("/log", "Logs")]),
]

CSS = """
:root{--page:#202020;--header:#2a2a2a;--header-text:#e1e2e3;--sidebar:#2a2a2a;--sidebar-text:#e1e2e3;
--sidebar-active:#333;--toolbar:#262626;--toolbar-text:#e1e2e3;--toolbar-hover:#3a3a3a;--card:#333;--card-hover:#3a3a3a;
--card-shadow:#111;--text:#ccc;--heading:#e1e2e3;--muted:#909293;--border:#3a3a3a;--legend-border:#555;--input:#333;
--input-border:#555;--accent:#35c5f4;--primary:#5d9cec;--success:#27c24c;--danger:#f05050;--warning:#ffa500;
--default-btn:#333;--default-btn-border:#555;--row-hover:rgba(255,255,255,.06);--modal:#2a2a2a;--overlay:rgba(0,0,0,.6);
--progress-bg:#4a4a4a;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--page:#f5f7fa;--header:#35c5f4;--header-text:#fff;--sidebar:#3a3f51;
--sidebar-text:#e1e2e3;--sidebar-active:#4b5163;--toolbar:#4f566f;--toolbar-text:#e1e2e3;--toolbar-hover:#5d6580;
--card:#fff;--card-hover:#fafbfc;--card-shadow:#ccc;--text:#515253;--heading:#3a3f51;--muted:#909293;--border:#eee;
--legend-border:#e5e5e5;--input:#fff;--input-border:#dde6e9;--default-btn:#fff;--default-btn-border:#eaeaea;
--row-hover:#fafbfc;--modal:#fff;--overlay:rgba(0,0,0,.4);--progress-bg:#ddd;color-scheme:light}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--page);color:var(--text);font:14px/1.528571429 "Roboto","Helvetica Neue",Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.icon{flex:none;vertical-align:middle}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.header{position:fixed;top:0;left:0;right:0;height:60px;background:var(--header);color:var(--header-text);display:flex;
align-items:center;z-index:20;padding-top:env(safe-area-inset-top,0px)}
.logo{flex:0 0 210px;padding-left:20px;display:flex;align-items:center}.logo:hover{text-decoration:none}
.logo .mark{display:grid;place-items:center}
.header form.search{display:flex;align-items:center;gap:8px;flex:0 1 380px;color:var(--header-text)}
.header input.search{flex:1;min-width:0;background:transparent;border:0;border-bottom:1px solid rgba(255,255,255,.5);
color:var(--header-text);padding:4px 2px;font:inherit;font-size:16px;outline:none;border-radius:0;height:auto}
.header input.search::placeholder{color:rgba(255,255,255,.6)}
.header .right{margin-left:auto;padding-right:15px;display:flex;align-items:center}
.header .hbtn{background:none;border:0;color:var(--header-text);cursor:pointer;width:40px;height:40px;display:grid;
place-items:center;border-radius:4px}
.header .hbtn:hover{color:var(--accent);background:rgba(255,255,255,.06)}
@media (prefers-color-scheme:light){.header .hbtn:hover{color:#fff;background:rgba(0,0,0,.08)}}
.menu{position:relative}.menu .items{display:none;position:absolute;right:0;top:44px;min-width:180px;background:var(--modal);
border-radius:4px;box-shadow:0 5px 15px rgba(0,0,0,.4);padding:4px 0;z-index:30}
.menu.open .items{display:block}
.menu .items a,.menu .items button{display:flex;align-items:center;gap:10px;width:100%;padding:8px 16px;color:var(--text);
background:none;border:0;font:inherit;cursor:pointer;text-align:left}
.menu .items a:hover,.menu .items button:hover{background:var(--row-hover);color:var(--accent);text-decoration:none}
.menu .items hr{border:0;border-top:1px solid var(--border);margin:4px 0}
.menu-toggle{display:none!important}
.sidebar{position:fixed;top:60px;bottom:0;left:0;width:210px;background:var(--sidebar);color:var(--sidebar-text);
overflow-y:auto;z-index:10;padding:0 0 env(safe-area-inset-bottom,0px)}
.sidebar a{display:flex;align-items:center;color:inherit;padding:12px 24px;border-left:3px solid transparent;font-size:14px}
.sidebar a .icon{width:18px;margin-right:12px}
.sidebar a:hover{text-decoration:none;color:var(--accent)}
.sidebar .section.active>a{border-left-color:var(--accent);background:var(--sidebar-active);color:var(--accent)}
.sidebar .sub{display:none;background:var(--sidebar-active);padding-bottom:6px}
.sidebar .section.active .sub{display:block}
.sidebar .sub a{padding:6px 24px 6px 54px;border-left:3px solid var(--accent)}
.sidebar .sub a.current{color:var(--accent)}
.main{margin-left:210px;padding-top:60px;min-height:100vh;display:flex;flex-direction:column}
.toolbar{position:sticky;top:60px;z-index:5;height:60px;background:var(--toolbar);color:var(--toolbar-text);display:flex;
align-items:center;gap:0;padding:0 20px;overflow-x:auto}
.toolbar form{display:contents}
.toolbar .sep{width:1px;height:40px;margin:0 20px;background:#888;opacity:.5;flex:none}
.tool{display:inline-flex;flex-direction:column;align-items:center;justify-content:center;width:60px;height:60px;padding:0;
background:none;border:0;color:inherit;font:inherit;font-size:11px;line-height:1.2;cursor:pointer;text-align:center;flex:none}
.tool .icon{margin-bottom:3px}
.tool:hover{color:var(--accent);text-decoration:none}
.tool.on{color:var(--warning)}
.content{padding:20px;flex:1;width:100%}
main.content h1{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.legend,main.content h2{display:block;width:100%;margin:0 0 21px;padding:0;font-size:21px;font-weight:400;line-height:1.5;
color:var(--heading);border:0;border-bottom:1px solid var(--legend-border)}
main.content h2:not(:first-child){margin-top:30px}
.muted{color:var(--muted)}.bad{color:var(--danger)}.good{color:var(--success)}.warn{color:var(--warning)}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;max-width:100%}
th{text-align:left;font-weight:400;color:var(--text);padding:8px;white-space:nowrap}
td{padding:8px;border-top:1px solid var(--border);line-height:1.52857143;vertical-align:middle}
tbody tr:hover td{background:var(--row-hover)}
td.icon-cell{width:36px;text-align:center}
.label{display:inline-block;margin:2px 2px 2px 0;padding:1px 5px 2px;border:1px solid var(--default-btn-border);
border-radius:2px;background:var(--default-btn);color:var(--text);font-size:12px;line-height:1.4;white-space:nowrap}
.label.ok{background:rgba(39,194,76,.14);border-color:rgba(39,194,76,.45);color:#7ed99a}
.label.bad{background:rgba(240,80,80,.14);border-color:rgba(240,80,80,.45);color:#f39a9a}
.label.warn{background:rgba(255,165,0,.14);border-color:rgba(255,165,0,.45);color:#ffc266}
.label.info{background:rgba(93,156,236,.14);border-color:rgba(93,156,236,.45);color:#9cc2f3}
@media (prefers-color-scheme:light){.label.ok{color:#1e7a35}.label.bad{color:#b52b2b}.label.warn{color:#8a5a00}
.label.info{color:#2f66b0}}
.label.outline{background:transparent;color:var(--muted)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:6px 16px;border-radius:4px;
border:1px solid var(--default-btn-border);background:var(--default-btn);color:var(--text);font:inherit;cursor:pointer;
line-height:1.52857143;white-space:nowrap}
.btn:hover{filter:brightness(1.12);text-decoration:none}
.btn.primary{background:var(--primary);border-color:#5899eb;color:#fff}
.btn.success{background:var(--success);border-color:#29c84e;color:#fff}
.btn.danger{background:var(--danger);border-color:#f05050;color:#fff}
.btn.small{padding:1px 8px;font-size:12px}
.btn.icon-only{padding:4px 7px;background:none;border-color:transparent;color:var(--text)}
.btn.icon-only:hover{color:var(--accent)}
.form-group{display:flex;flex-wrap:wrap;margin-bottom:20px}
.form-group>label{flex:0 0 250px;text-align:right;margin-right:20px;padding-top:6px;font-weight:700;color:var(--text);
line-height:1.4}
.form-group .field{flex:0 1 600px;min-width:0}
.form-group .help{display:flex;gap:6px;align-items:flex-start;color:var(--muted);font-size:13px;margin-top:5px;line-height:1.4}
.form-group .help .icon{color:var(--primary);margin-top:1px}
.form-group.advanced>label{color:var(--warning)}
body:not(.show-advanced) .advanced{display:none}
input[type=text],input[type=password],input[type=number],input[type=url],input:not([type]),select,textarea{
width:100%;height:35px;background:var(--input);border:1px solid var(--input-border);border-radius:4px;color:var(--text);
padding:6px 16px;font:inherit;box-shadow:inset 0 1px 1px rgba(0,0,0,.075)}
textarea{height:auto;min-height:70px}
input:focus,select:focus,textarea:focus{outline:none;border-color:#66afe9;box-shadow:inset 0 1px 1px rgba(0,0,0,.075),0 0 8px rgba(102,175,233,.6)}
input[type=checkbox]{width:20px;height:20px;accent-color:var(--primary);vertical-align:middle;margin:6px 6px 6px 0}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:10px}
.card{background:var(--card);border-radius:3px;padding:10px;box-shadow:0 0 10px 1px var(--card-shadow)}
.card.click{cursor:pointer;text-align:left;font:inherit;color:inherit;border:0;width:290px;min-height:90px;
display:block;margin:10px 0 0}
.card.click:hover{text-decoration:none;box-shadow:0 0 12px 2px var(--card-shadow)}
.card .title{font-size:16px;font-weight:300;color:var(--heading);margin-bottom:5px}
.card.add{display:grid;place-items:center;color:var(--muted)}
.card.add:hover{color:var(--accent)}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:10px}
.stat{min-width:160px}.stat .n{font-size:24px;color:var(--heading)}.stat .k{color:var(--muted)}
.progress{position:relative;height:20px;background:var(--progress-bg);border-radius:4px;overflow:hidden;min-width:120px}
.progress div{height:100%;background:var(--primary)}
.progress div.warn{background:var(--warning)}.progress div.bad{background:var(--danger)}
.progress span{position:absolute;inset:0;text-align:center;font-size:12px;line-height:20px;color:#fff}
.progress.thin{height:5px}
.bars{display:flex;gap:4px;align-items:flex-end;height:150px}
.bar{flex:1 0 1.6rem;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}
.bar .f{width:100%;background:var(--primary);border-radius:2px 2px 0 0;min-height:1px}.bar span,.bar small{font-size:11px}
.bar small{color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;margin:0}
code{font:12px ui-monospace,Menlo,Consolas,monospace;background:var(--card);padding:1px 4px;border-radius:2px;
word-break:break-all}
.alert{border-radius:4px;padding:15px;margin-bottom:20px;border:1px solid transparent;color:#fff}
.alert{background:#1e5f87;border-color:#2a7aab}
.alert.ok{background:#1d5b2c;border-color:#27c24c}.alert.bad{background:#7a2727;border-color:#f05050}
.alert.warn{background:#7a5a10;border-color:#ffa500}
@media (prefers-color-scheme:light){.alert{background:#d9edf7;border-color:#bce8f1;color:#31708f}
.alert.ok{background:#dff0d8;border-color:#d6e9c6;color:#3c763d}.alert.bad{background:#f2dede;border-color:#ebccd1;color:#a94442}
.alert.warn{background:#fcf8e3;border-color:#faebcc;color:#8a6d3b}}
.alert a{color:inherit;text-decoration:underline}
dialog{background:var(--modal);color:var(--text);border:0;border-radius:6px;padding:0;width:min(800px,calc(100vw - 32px));
box-shadow:0 5px 15px rgba(0,0,0,.5)}
dialog::backdrop{background:var(--overlay)}
dialog .mh{padding:15px 50px 15px 30px;font-size:18px;color:var(--heading);border-bottom:1px solid var(--border)}
dialog .mb{padding:30px;max-height:70vh;overflow-y:auto}
dialog .mf{padding:15px 30px;border-top:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
dialog .mf .spacer{flex:1}
dialog .form-group>label{flex-basis:160px}
.test-result{font-size:13px;margin-top:4px}
.settings-list a.item{display:block;padding:12px 0;color:inherit;text-decoration:none}
.settings-list a.item .t{font-size:21px;color:var(--accent);font-weight:400}
.settings-list a.item:hover .t{text-decoration:underline}
.settings-list a.item .d{color:var(--muted);margin-top:2px}
.login{max-width:440px;margin:10vh auto 0;padding:0 16px}
.login .panel{background:var(--card);border-radius:4px;overflow:hidden;box-shadow:0 0 10px var(--card-shadow)}
.login .ph{background:var(--header);color:var(--header-text);padding:20px;display:flex;align-items:center;gap:12px;
font-size:20px}
.login .pb{padding:20px}
.login .form-group{display:block}.login .form-group>label{text-align:left;padding:0 0 4px;display:block;margin:0}
.login .form-group .field{max-width:none}
@media (max-width:768px){
.menu-toggle{display:grid!important}.logo{flex:0 0 auto;padding-left:6px}
.sidebar{transform:translateX(-100%);transition:transform .15s}.sidebar.open{transform:none}
.main{margin-left:0}.form-group>label{flex-basis:100%;text-align:left;margin:0;padding:0 0 4px}
.header form.search{flex:1}.content{padding:16px}.card.click{width:100%}.toolbar{padding:0 8px}
.toolbar .sep{margin:0 8px}}
"""

JS = """
(function(){
  var body=document.body;
  try{if(localStorage.getItem('mc-advanced')==='1')body.classList.add('show-advanced')}catch(e){}
  document.querySelectorAll('[data-advanced]').forEach(function(b){
    if(body.classList.contains('show-advanced'))b.classList.add('on');
    b.addEventListener('click',function(){var on=body.classList.toggle('show-advanced');b.classList.toggle('on',on);
      try{localStorage.setItem('mc-advanced',on?'1':'0')}catch(e){}});});
  document.querySelectorAll('[data-open]').forEach(function(b){b.addEventListener('click',function(){
    var d=document.getElementById(b.getAttribute('data-open'));if(d)d.showModal();});});
  document.querySelectorAll('[data-close]').forEach(function(b){b.addEventListener('click',function(){
    b.closest('dialog').close();});});
  document.querySelectorAll('[data-confirm]').forEach(function(f){f.addEventListener('submit',function(e){
    if(!window.confirm(f.getAttribute('data-confirm')))e.preventDefault();});});
  document.querySelectorAll('[data-test]').forEach(function(b){b.addEventListener('click',function(){
    var form=b.form||b.closest('form'),out=form.querySelector('.test-result');out.textContent='Testing…';out.className='test-result muted';
    fetch(b.getAttribute('data-test'),{method:'POST',body:new URLSearchParams(new FormData(form)),credentials:'same-origin'})
      .then(function(r){return r.json()}).then(function(j){out.textContent=j.message;out.className='test-result '+(j.ok?'good':'bad');})
      .catch(function(){out.textContent='Test failed';out.className='test-result bad';});});});
  document.querySelectorAll('.menu > .hbtn').forEach(function(b){b.addEventListener('click',function(e){
    e.stopPropagation();b.parentNode.classList.toggle('open');});});
  document.addEventListener('click',function(){document.querySelectorAll('.menu.open').forEach(function(m){
    m.classList.remove('open')});});
  var t=document.querySelector('.menu-toggle'),s=document.querySelector('.sidebar');
  if(t&&s)t.addEventListener('click',function(){s.classList.toggle('open')});
})();
"""


def layout(title: str, body: str, *, path: str, toolbar: str = "", refresh: int = 0, logout: bool = False,
           search: str = "") -> str:
    """A full page: header, sidebar (the section of `path` open), toolbar and content."""
    nav = []
    for key, label, ico, first, pages in SECTIONS:
        active = any(path == p or path.startswith(p + "/") for p, _ in pages) or path == first or (
            key == "activity" and path == "/")
        subs = "".join(f'<a href="{p}" class="{"current" if path == p or (p == "/queue" and path == "/") else ""}">'
                       f'{esc(name)}</a>' for p, name in pages) if len(pages) > 1 else ""
        nav.append(f'<div class="section{" active" if active else ""}"><a href="{first}">{icon(ico)}{esc(label)}</a>'
                   f'<div class="sub">{subs}</div></div>')
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    out = (f'<hr><form method="post" action="/logout"><button>{icon("logout", 16)} Log out</button></form>'
           if logout else "")
    system_menu = (f'<div class="menu"><button class="hbtn" type="button" title="System">{icon("system", 20)}</button>'
                   f'<div class="items"><a href="/system/status">{icon("activity", 16)} Status</a>'
                   f'<a href="/system/tasks">{icon("play", 16)} Tasks</a><a href="/log">{icon("library", 16)} Logs</a>'
                   f'<a href="/hardware">{icon("chip", 16)} Hardware</a>{out}</div></div>')
    bar = f'<div class="toolbar">{toolbar}</div>' if toolbar else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1,viewport-fit=cover">{meta}<title>{esc(title)} - '
            f'media-converterr</title><style>{CSS}</style></head><body>'
            f'<header class="header"><button class="hbtn menu-toggle" type="button" title="Menu">{icon("menu")}'
            f'</button><a class="logo" href="/" title="media-converterr">{LOGO}</a>'
            f'<form class="search" method="get" action="/files">{icon("search")}<input class="search" name="q" '
            f'value="{esc(search)}" placeholder="Search" aria-label="Search files"></form>'
            f'<div class="right">{system_menu}</div></header>'
            f'<nav class="sidebar">{"".join(nav)}</nav>'
            f'<div class="main">{bar}<main class="content">{body}</main></div><script>{JS}</script></body></html>')


def bare(title: str, body: str) -> str:
    """A page without navigation (login, first-run setup), like Sonarr's login panel."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{esc(title)} - media-converterr'
            f'</title><style>{CSS}</style></head><body><div class="login">{body}</div><script>{JS}</script>'
            f'</body></html>')


def panel(title: str, inner: str) -> str:
    return (f'<div class="panel"><div class="ph"><span class="logo" style="flex:none;padding:0">{LOGO}</span>'
            f'{esc(title)}</div><div class="pb">{inner}</div></div>')


def tool(label: str, ico: str, *, href: str = "", action: str = "", form: str = "", attrs: str = "",
         confirm: str = "") -> str:
    """A toolbar button: a link, a one-button POST form, a submit button for a form elsewhere, or a plain button."""
    inner = f"{icon(ico, 21)}<span>{esc(label)}</span>"
    if href:
        return f'<a class="tool" href="{esc(href)}" {attrs}>{inner}</a>'
    if action:
        c = f' data-confirm="{esc(confirm)}"' if confirm else ""
        return f'<form method="post" action="{esc(action)}"{c}><button class="tool" {attrs}>{inner}</button></form>'
    if form:
        return f'<button class="tool" form="{esc(form)}" {attrs}>{inner}</button>'
    return f'<button class="tool" type="button" {attrs}>{inner}</button>'


def advanced_toggle() -> str:
    return tool("Show Advanced", "advanced", attrs="data-advanced")


def form_group(label: str, field: str, help_text: str = "", *, advanced: bool = False) -> str:
    help_html = f'<div class="help">{icon("info", 14)}<span>{help_text}</span></div>' if help_text else ""
    return (f'<div class="form-group{" advanced" if advanced else ""}"><label>{esc(label)}</label>'
            f'<div class="field">{field}{help_html}</div></div>')


def text_input(name: str, value="", *, kind: str = "text", placeholder: str = "", attrs: str = "") -> str:
    return (f'<input type="{kind}" name="{esc(name)}" value="{esc(value)}" placeholder="{esc(placeholder)}" '
            f'{attrs}>')


def select(name: str, options: Iterable[tuple], current) -> str:
    opts = "".join(f'<option value="{esc(v)}"{" selected" if str(v) == str(current) else ""}>{esc(label)}</option>'
                   for v, label in options)
    return f'<select name="{esc(name)}">{opts}</select>'


def checkbox(name: str, checked: bool, text: str = "") -> str:
    return f'<label><input type="checkbox" name="{esc(name)}"{" checked" if checked else ""}> {esc(text)}</label>'


def label(text: str, kind: str = "") -> str:
    return f'<span class="label {kind}">{esc(text)}</span>'


def alert(text: str, kind: str = "") -> str:
    return f'<div class="alert {kind}">{text}</div>'


def table(headers: Iterable[str], rows: Iterable[Iterable[str]], empty: str = "None.") -> str:
    rows = list(rows)
    if not rows:
        return f'<p class="muted">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def dialog(dialog_id: str, title: str, body: str, *, action: str, delete_action: str = "", delete_fields: str = "",
           test_url: str = "", form_id: Optional[str] = None) -> str:
    """An edit dialog in the Sonarr style: form fields, then Delete (left), Test, Cancel and Save (right)."""
    form_id = form_id or f"{dialog_id}-form"
    delete = (f'<form method="post" action="{esc(delete_action)}" data-confirm="Delete {esc(title)}?" '
              f'style="display:inline">{delete_fields}<button class="btn danger">{icon("trash", 16)} Delete</button>'
              f'</form>' if delete_action else "")
    test = (f'<button type="button" form="{esc(form_id)}" class="btn" data-test="{esc(test_url)}">'
            f'{icon("check", 16)} Test</button>' if test_url else "")
    return (f'<dialog id="{esc(dialog_id)}"><form method="post" action="{esc(action)}" id="{esc(form_id)}">'
            f'<div class="mh">{esc(title)}</div><div class="mb">{body}<div class="test-result"></div></div>'
            f'</form><div class="mf">{delete}<span class="spacer"></span>'
            f'{test}'
            f'<button type="button" class="btn" data-close>Cancel</button>'
            f'<button class="btn primary" form="{esc(form_id)}">{icon("save", 16)} Save</button></div></dialog>')


def status_icon(kind: str, title: str = "") -> str:
    """Coloured status icon as Sonarr shows in its tables: info, warning, error, ok, clock (queued)."""
    color = {"info": "var(--primary)", "warning": "var(--warning)", "error": "var(--danger)", "ok": "var(--success)",
             "clock": "var(--muted)"}.get(kind, "var(--muted)")
    return f'<span title="{esc(title)}" style="color:{color}">{icon(kind, 16)}</span>'


def progress(pct: Optional[float], text: str = "", kind: str = "") -> str:
    if pct is None:
        return '<span class="muted">–</span>'
    pct = max(0.0, min(100.0, pct))
    return f'<div class="progress"><div class="{kind}" style="width:{pct:.0f}%"></div><span>{esc(text)}</span></div>'
