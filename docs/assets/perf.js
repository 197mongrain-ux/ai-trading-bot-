/* perf.js - shared all-time performance panels for Chris's boards (HL, Propr, scalp paper).
   Layout modelled on pro trading-journal dashboards: headline KPI cards, cumulative equity with
   daily bars, underwater drawdown, month table, breakdowns (symbol / side / weekday / session), trade list.
   Pure client-side, computes only from the closed trades the page passes in (real fills / engine trades).
   PERF.top(el, opts)    -> hero + objectives + KPI cards + equity + underwater
   PERF.bottom(el, opts) -> months + breakdowns + trade list
   trade: {day:'YYYY-MM-DD' (close, ET), open_et:'YYYY-MM-DD HH:MM', close_et, coin, side:'long'|'short', v:number, r:number|null, fees:number|null, href?:string}
   opts:  {unit:'$'|'R', start?:number (start balance), today:'YYYY-MM-DD', alt?:t=>string, altLabel?, objectives?:[{name,limit,result,used,status,note}], note?:string} */
(function(){
const css=`
.pf{display:flex;flex-direction:column;gap:1rem}
.pf-hero{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,2fr);gap:1rem;border:1px solid #1a1a1a;border-radius:10px;padding:1rem 1.1rem;background:#0e0e0e}
.pf-hero.up{background:linear-gradient(135deg,#03170a 0%,#0b0b0b 70%);border-color:#14451f}.pf-hero.dn{background:linear-gradient(135deg,#1c0709 0%,#0b0b0b 70%);border-color:#4a161b}
@media(max-width:760px){.pf-hero{grid-template-columns:1fr}}
.pf-lbl{color:#8b8b8b;font-size:10px;text-transform:uppercase;letter-spacing:.08em}
.pf-big{font-family:var(--mono,ui-monospace,monospace);font-size:2.1rem;font-weight:750;line-height:1.1;margin:.2rem 0 .35rem;font-variant-numeric:tabular-nums}
.pf-yes{display:inline-block;font-family:var(--mono,monospace);font-size:11px;font-weight:700;letter-spacing:.06em;padding:.25rem .6rem;border-radius:4px}
.pf-yes.y{color:#00c805;border:1px solid #1a5c2a;background:#06140a}.pf-yes.n{color:#ff4d4f;border:1px solid #7a3338;background:#1a0a0c}
.pf-hs{color:#8b8b8b;font-size:11.5px;margin-top:.45rem;line-height:1.5;font-family:var(--mono,monospace)}
.pf-hgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.5rem .9rem;align-content:center}
@media(max-width:480px){.pf-hgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
.pf-hgrid .v{font-family:var(--mono,monospace);font-size:1.05rem;font-weight:650;margin-top:.1rem;font-variant-numeric:tabular-nums}.pf-hgrid .s{color:#5c5c5c;font-size:10px;font-family:var(--mono,monospace)}
.pf-cards{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.6rem}
@media(max-width:1000px){.pf-cards{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:520px){.pf-cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
.pf-card{background:#0e0e0e;border:1px solid #1a1a1a;border-radius:8px;padding:.7rem .8rem}
.pf-card .v{font-family:var(--mono,monospace);font-size:1.15rem;font-weight:650;margin-top:.2rem;font-variant-numeric:tabular-nums}.pf-card .s{color:#5c5c5c;font-size:10px;margin-top:.2rem;font-family:var(--mono,monospace)}
.pf-ring{display:flex;align-items:center;gap:.55rem}
.pf-box{background:#0e0e0e;border:1px solid #1a1a1a;border-radius:8px;overflow:hidden}
.pf-bh{display:flex;align-items:center;gap:.6rem;padding:.6rem .9rem;border-bottom:1px solid #1a1a1a;background:#0a0a0a;flex-wrap:wrap}
.pf-bh h2{margin:0;font-size:12px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:#8b8b8b}.pf-bh .m{color:#5c5c5c;font-size:11px;margin-left:auto;font-family:var(--mono,monospace)}
.pf-bb{padding:.8rem .9rem}.pf-bb.np{padding:0}
.pf-2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}@media(max-width:860px){.pf-2{grid-template-columns:1fr}}
.pf table{width:100%;border-collapse:collapse;font-size:12px}
.pf th,.pf td{padding:.42rem .6rem;border-bottom:1px solid #161616;text-align:left;white-space:nowrap}
.pf th{color:#8b8b8b;font-size:10px;text-transform:uppercase;letter-spacing:.06em;font-weight:600;background:#0a0a0a}
.pf td.n,.pf th.n{text-align:right;font-family:var(--mono,monospace);font-variant-numeric:tabular-nums}
.pf tr:last-child td{border-bottom:0}.pf tr.tot td{border-top:1px solid #262626;font-weight:700}
.pf .tw{overflow-x:auto}
.pf .p{color:#00c805}.pf .q{color:#ff4d4f}.pf .d{color:#5c5c5c}.pf .a{color:#e6c07b}
.pf-bar{position:relative;height:8px;background:#151515;border-radius:4px;min-width:70px;overflow:hidden}.pf-bar i{position:absolute;top:0;bottom:0;border-radius:4px}
.pf-prog{height:7px;background:#151515;border-radius:4px;overflow:hidden;margin-top:.35rem}.pf-prog i{display:block;height:100%;border-radius:4px}
.pf-st{font-family:var(--mono,monospace);font-size:10px;padding:.12rem .45rem;border-radius:999px;border:1px solid #262626}
.pf-st.ok{color:#00c805;border-color:#1a5c2a;background:#06140a}.pf-st.go{color:#6ea8fe;border-color:#22406e;background:#07101c}.pf-st.bad{color:#ff4d4f;border-color:#7a3338;background:#1a0a0c}
.pf-tabs{display:flex;gap:.3rem;margin-left:auto}.pf-tabs button{font-family:var(--mono,monospace);font-size:11px;background:#111;color:#e8e8e8;border:1px solid #262626;border-radius:5px;padding:.25rem .55rem;cursor:pointer}.pf-tabs button.on{border-color:#6ea8fe;color:#6ea8fe}
.pf-note{color:#5c5c5c;font-size:11px;line-height:1.5}
.pf a{color:#93c5fd;text-decoration:none}
.pf-more{display:block;width:100%;background:#0a0a0a;border:0;border-top:1px solid #1a1a1a;color:#8b8b8b;padding:.55rem;font-family:var(--mono,monospace);font-size:11px;cursor:pointer}
`;
if(!document.getElementById("pf-css")){const s=document.createElement("style");s.id="pf-css";s.textContent=css;document.head.appendChild(s);}
const E=1e-9, esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const C=v=>v==null||isNaN(v)?"d":(v>0.005?"p":(v<-0.005?"q":""));
function M(v,unit,signed=true){ if(v==null||isNaN(v)||!isFinite(v)) return "—";
  if(unit==="R") return (v>0&&signed?"+":v<0?"−":"")+Math.abs(v).toFixed(2)+"R";
  const a=Math.abs(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2}); return (v<0?"−$":(signed&&v>0?"+$":"$"))+a; }
const R=v=>v==null||isNaN(v)?"—":(v>0?"+":v<0?"−":"")+Math.abs(v).toFixed(2)+"R";
const P=v=>v==null||isNaN(v)?"—":v.toFixed(0)+"%";
const F=v=>v==null||isNaN(v)?"—":(!isFinite(v)?"∞":v.toFixed(2));
const addDay=(d,k)=>{const x=new Date(d+"T12:00:00Z");x.setUTCDate(x.getUTCDate()+k);return x.toISOString().slice(0,10);};
const WD=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const wdOf=d=>(new Date(d+"T12:00:00Z").getUTCDay()+6)%7;
const SESS=[["Overnight 00–07",0,7],["Pre-market 07–09:30",7,9.5],["Open 09:30–11",9.5,11],["Midday 11–14",11,14],["Afternoon 14–16",14,16],["Evening 16–24",16,24]];
function sessOf(et){ if(!et||et.length<16) return null; const h=+et.slice(11,13)+(+et.slice(14,16))/60; const s=SESS.find(x=>h>=x[1]&&h<x[2]); return s?s[0]:null; }

function stats(T){
  const n=T.length, W=T.filter(t=>t.v>0.005), L=T.filter(t=>t.v<-0.005);
  const gw=W.reduce((a,t)=>a+t.v,0), gl=-L.reduce((a,t)=>a+t.v,0), net=T.reduce((a,t)=>a+t.v,0);
  const rs=T.filter(t=>t.r!=null&&!isNaN(t.r)), fees=T.reduce((a,t)=>a+(t.fees||0),0);
  let ws=0,ls=0,mw=0,ml=0; for(const t of T){ if(t.v>0.005){ws++;ls=0;} else if(t.v<-0.005){ls++;ws=0;} mw=Math.max(mw,ws); ml=Math.max(ml,ls); }
  return {n,w:W.length,l:L.length,net,gw,gl,pf:gl>E?gw/gl:(gw>E?Infinity:null),wr:W.length+L.length?100*W.length/(W.length+L.length):null,
    aw:W.length?gw/W.length:null,al:L.length?-gl/L.length:null,exp:n?net/n:null,ar:rs.length?rs.reduce((a,t)=>a+t.r,0)/rs.length:null,rn:rs.length,
    rsum:rs.reduce((a,t)=>a+t.r,0),fees,mw,ml};
}
function build(T0,o){
  const T=T0.slice().sort((a,b)=>(a.close_ms??0)-(b.close_ms??0)||(a.close_et||"").localeCompare(b.close_et||""));
  const S=stats(T), st=o.start||0;
  // trade-by-trade equity & drawdown (closed PnL), peak starts at 0 (= start balance)
  let cum=0,pk=0,dd=0,ddDay=null,ddPk=0; for(const t of T){ cum+=t.v; if(cum>pk) pk=cum; if(cum-pk<dd){dd=cum-pk;ddDay=t.day;ddPk=pk;} }
  S.dd=dd; S.ddDay=ddDay; S.cur=cum-pk; S.peak=pk; S.ddPct=st?100*dd/(st+ddPk):null; S.rf=dd<-E?S.net/-dd:null;
  // daily
  const dm={}; for(const t of T){ (dm[t.day]??={v:0,n:0,w:0,l:0}); const d=dm[t.day]; d.v+=t.v; d.n++; d.w+=t.v>0.005; d.l+=t.v<-0.005; }
  const dk=Object.keys(dm).sort(); const dv=dk.map(k=>dm[k].v);
  S.days=dk.length; S.gd=dv.filter(v=>v>0.005).length; S.dwr=dk.length?100*S.gd/dk.length:null;
  S.best=dk.length?dk.reduce((a,k)=>dm[k].v>dm[a].v?k:a):null; S.worst=dk.length?dk.reduce((a,k)=>dm[k].v<dm[a].v?k:a):null;
  S.dm=dm; S.first=dk[0]||null; S.T=T;
  // daily series first day -> today (calendar days)
  const ser=[]; if(S.first){ let d=S.first, c=0, p=0; const end=(o.today&&o.today>dk[dk.length-1])?o.today:dk[dk.length-1];
    for(let i=0;i<4000&&d<=end;i++){ const v=dm[d]?.v||0; c+=v; p=Math.max(p,c); ser.push({d,v,c,u:c-p,n:dm[d]?.n||0}); d=addDay(d,1);} }
  S.ser=ser; return S;
}
function chartEq(S,o){
  const s=S.ser; if(!s.length) return '<div class="pf-note">No closed trades yet.</div>';
  const W=1000,H=300,pl=62,pr=14,pt=14,pb=26, U=o.unit;
  const cs=s.map(x=>x.c), bs=s.map(x=>x.v); const mn=Math.min(0,...cs,...bs), mx=Math.max(0,...cs,...bs), rg=(mx-mn)||1;
  const X=i=>pl+(i+0.5)/s.length*(W-pl-pr), Y=v=>pt+(mx-v)/rg*(H-pt-pb), bw=Math.max(2,Math.min(26,(W-pl-pr)/s.length*0.6));
  let g=""; for(let j=0;j<=4;j++){const v=mn+rg*j/4; g+=`<line x1=${pl} x2=${W-pr} y1=${Y(v).toFixed(1)} y2=${Y(v).toFixed(1)} stroke="#161616"/><text x=${pl-8} y=${(Y(v)+4).toFixed(1)} fill="#5c5c5c" font-size=11 text-anchor=end font-family="monospace">${M(v,U,false).replace("+","")}</text>`;}
  g+=`<line x1=${pl} x2=${W-pr} y1=${Y(0)} y2=${Y(0)} stroke="#3a3a3a" stroke-dasharray="4 3"/>`;
  s.forEach((x,i)=>{ if(Math.abs(x.v)>E){ const y0=Y(0),y1=Y(x.v); g+=`<rect x=${(X(i)-bw/2).toFixed(1)} y=${Math.min(y0,y1).toFixed(1)} width=${bw.toFixed(1)} height=${Math.max(1,Math.abs(y1-y0)).toFixed(1)} fill="${x.v>0?"#00c805":"#ff4d4f"}" opacity=.45><title>${x.d}: ${M(x.v,U)} (${x.n} trade${x.n>1?"s":""})</title></rect>`; } });
  const last=s[s.length-1].c, col=last>=0?"#00c805":"#ff4d4f", pts=s.map((x,i)=>`${X(i).toFixed(1)},${Y(x.c).toFixed(1)}`).join(" ");
  g+=`<defs><linearGradient id="pfg" x1=0 x2=0 y1=0 y2=1><stop offset=0 stop-color="${col}" stop-opacity=.28 /><stop offset=1 stop-color="${col}" stop-opacity=0 /></linearGradient></defs>`;
  g+=`<polygon points="${X(0).toFixed(1)},${Y(0).toFixed(1)} ${pts} ${X(s.length-1).toFixed(1)},${Y(0).toFixed(1)}" fill="url(#pfg)"/><polyline fill=none stroke="${col}" stroke-width=2.2 stroke-linejoin=round points="${pts}"/>`;
  g+=s.map((x,i)=>`<circle cx=${X(i).toFixed(1)} cy=${Y(x.c).toFixed(1)} r=${x.n?3:0} fill="${col}"><title>${x.d} · day ${M(x.v,U)} · cumulative ${M(x.c,U)}${o.start&&U!=="R"?" · balance "+M(o.start+x.c,U,false):""}</title></circle>`).join("");
  const step=Math.max(1,Math.ceil(s.length/8)); s.forEach((x,i)=>{ if(i%step===0||i===s.length-1) g+=`<text x=${X(i).toFixed(1)} y=${H-6} fill="#8b8b8b" font-size=11 text-anchor=middle font-family="monospace">${x.d.slice(5)}</text>`; });
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" role="img" aria-label="cumulative PnL">${g}</svg>`;
}
function chartUw(S,o){
  const s=S.ser; if(!s.length) return "";
  const W=1000,H=120,pl=62,pr=14,pt=8,pb=20,U=o.unit; const mn=Math.min(...s.map(x=>x.u),-E), X=i=>pl+(i+0.5)/s.length*(W-pl-pr), Y=v=>pt+(v/mn)*(H-pt-pb);
  const pts=s.map((x,i)=>`${X(i).toFixed(1)},${Y(x.u).toFixed(1)}`).join(" ");
  let g=`<line x1=${pl} x2=${W-pr} y1=${pt} y2=${pt} stroke="#3a3a3a" stroke-dasharray="4 3"/><text x=${pl-8} y=${pt+4} fill="#5c5c5c" font-size=11 text-anchor=end font-family="monospace">0</text><text x=${pl-8} y=${H-pb+4} fill="#5c5c5c" font-size=11 text-anchor=end font-family="monospace">${M(mn,U,false)}</text>`;
  g+=`<polygon points="${X(0).toFixed(1)},${pt} ${pts} ${X(s.length-1).toFixed(1)},${pt}" fill="#ff4d4f" opacity=.22 /><polyline fill=none stroke="#ff4d4f" stroke-width=1.6 points="${pts}"/>`;
  g+=s.map((x,i)=>`<rect x=${(X(i)-3).toFixed(1)} y=${pt} width=6 height=${H-pt-pb} fill="transparent"><title>${x.d} · below peak ${M(x.u,U)}</title></rect>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" role="img" aria-label="drawdown">${g}</svg>`;
}
const card=(l,v,s,c)=>`<div class="pf-card"><div class="pf-lbl">${l}</div><div class="v ${c||""}">${v}</div><div class="s">${s||""}</div></div>`;
function donut(p){ if(p==null) return ""; const r=15,c=2*Math.PI*r; return `<svg width=38 height=38 viewBox="0 0 38 38"><circle cx=19 cy=19 r=${r} fill=none stroke="#3d1418" stroke-width=5 /><circle cx=19 cy=19 r=${r} fill=none stroke="#00c805" stroke-width=5 stroke-dasharray="${(c*p/100).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 19 19)"/></svg>`; }

function top(el,o){
  const S=build(o.trades||[],o), U=o.unit, up=S.net>0.005; el.__pf=S;
  const st=o.start, ret=st?100*S.net/st:null;
  let h=`<div class="pf">`;
  h+=`<div class="pf-hero ${S.n?(up?"up":"dn"):""}"><div><div class="pf-lbl">All-time net PnL${U==="R"?" (R, after fees)":" (after fees)"}</div>
   <div class="pf-big ${C(S.net)}">${M(S.net,U)}</div>
   ${S.n?`<span class="pf-yes ${up?"y":"n"}">PROFITABLE OVERALL: ${up?"YES":"NO"}</span>`:`<span class="pf-yes n">NO CLOSED TRADES YET</span>`}
   <div class="pf-hs">${S.n} closed trades · ${S.days} trading days${S.first?` · since ${S.first}`:""}${o.alt?`<br>${o.alt(S)}`:""}${ret!=null?`<br>${ret>=0?"+":""}${ret.toFixed(2)}% on ${M(st,U,false)} start`:""}${S.fees?` · fees paid ${M(S.fees,U,false)}`:""}</div></div>
   <div class="pf-hgrid">
    <div><div class="pf-lbl">Peak</div><div class="v p">${M(S.peak,U)}</div><div class="s">highest cumulative</div></div>
    <div><div class="pf-lbl">Now vs peak</div><div class="v ${C(S.cur)}">${M(S.cur,U)}</div><div class="s">current drawdown</div></div>
    <div><div class="pf-lbl">Max drawdown</div><div class="v q">${M(S.dd,U)}</div><div class="s">${S.ddPct!=null?S.ddPct.toFixed(2)+"% · ":""}${S.ddDay?"low on "+S.ddDay.slice(5):"—"}</div></div>
    <div><div class="pf-lbl">Best day</div><div class="v p">${S.best?M(S.dm[S.best].v,U):"—"}</div><div class="s">${S.best||""}</div></div>
    <div><div class="pf-lbl">Worst day</div><div class="v q">${S.worst?M(S.dm[S.worst].v,U):"—"}</div><div class="s">${S.worst||""}</div></div>
    <div><div class="pf-lbl">Green days</div><div class="v">${S.gd} / ${S.days}</div><div class="s">${P(S.dwr)} day win rate</div></div>
   </div></div>`;
  if(o.objectives&&o.objectives.length){
    h+=`<div class="pf-box"><div class="pf-bh"><h2>Challenge objectives</h2><span class="m">${esc(o.objTitle||"")}</span></div><div class="pf-bb np tw"><table><thead><tr><th>Objective</th><th class=n>Limit / target</th><th class=n>Result</th><th style="min-width:140px">Usage</th><th>Status</th></tr></thead><tbody>`+
      o.objectives.map(x=>{const u=Math.max(0,Math.min(100,x.used||0)); const col=x.kind==="target"?"#00c805":(u>=75?"#ff4d4f":u>=50?"#e6c07b":"#6ea8fe");
        const sc=x.status==="Passed"||x.status==="Safe"?"ok":x.status==="Breached"?"bad":"go";
        return `<tr><td>${esc(x.name)}<div class="d" style="font-size:10px;white-space:normal">${esc(x.note||"")}</div></td><td class=n>${x.limit}</td><td class=n>${x.result}</td><td><div class="pf-prog"><i style="width:${u}%;background:${col}"></i></div><div class="d" style="font-size:10px;margin-top:.2rem">${u.toFixed(1)}%${x.kind==="target"?" of target":" used"}</div></td><td><span class="pf-st ${sc}">${esc(x.status)}</span></td></tr>`;}).join("")+`</tbody></table></div></div>`;
  }
  h+=`<div class="pf-cards">
    ${card("Win rate",`<span class="pf-ring">${donut(S.wr)}${P(S.wr)}</span>`,`${S.w}W / ${S.l}L${S.n-S.w-S.l?` / ${S.n-S.w-S.l} BE`:""}`)}
    ${card("Profit factor",F(S.pf),`gross win ${M(S.gw,U,false)} / loss ${M(S.gl,U,false)}`,S.pf==null?"":(S.pf>=1?"p":"q"))}
    ${card("Avg win / avg loss",`<span class=p>${M(S.aw,U)}</span> <span class=d>/</span> <span class=q>${M(S.al,U)}</span>`,`payoff ${S.aw!=null&&S.al?F(S.aw/-S.al):"—"} : 1`)}
    ${card("Expectancy / trade",M(S.exp,U),`net ÷ ${S.n} trades`,C(S.exp))}
    ${card(U==="R"?"Avg R / trade":"Avg R-multiple",R(S.ar),S.rn?`sum ${R(S.rsum)} · ${S.rn} trades with a stop`:"no stop data",C(S.ar))}
    ${card("Streaks",`<span class=p>${S.mw}W</span> <span class=d>/</span> <span class=q>${S.ml}L</span>`,`longest win / loss run${S.rf!=null?` · recovery ${F(S.rf)}`:""}`)}
  </div>`;
  h+=`<div class="pf-box"><div class="pf-bh"><h2>All-time equity · daily</h2><span class="m">bars = daily net · line = cumulative${o.start&&U!=="R"?` (on ${M(o.start,U,false)} start)`:""}</span></div><div class="pf-bb">${chartEq(S,o)}</div>
    <div class="pf-bh" style="border-top:1px solid #1a1a1a"><h2>Drawdown (underwater)</h2><span class="m">distance below the running peak · max ${M(S.dd,U)}</span></div><div class="pf-bb">${chartUw(S,o)}</div></div>`;
  h+=`</div>`; el.innerHTML=h; return S;
}
function grp(T,key,order){ const m=new Map(); for(const t of T){ const k=key(t); if(k==null) continue; if(!m.has(k)) m.set(k,[]); m.get(k).push(t); }
  let ks=[...m.keys()]; if(order) ks=order.filter(k=>m.has(k)); else ks.sort((a,b)=>stats(m.get(b)).net-stats(m.get(a)).net); return ks.map(k=>[k,stats(m.get(k))]); }
function btable(title,rows,U,sub){
  const mx=Math.max(E,...rows.map(([,s])=>Math.abs(s.net)));
  return `<div class="pf-box"><div class="pf-bh"><h2>${title}</h2>${sub?`<span class="m">${sub}</span>`:""}</div><div class="pf-bb np tw"><table><thead><tr><th></th><th class=n>Trades</th><th class=n>Win %</th><th class=n>Net</th><th class=n>Avg</th><th class=n>PF</th><th style="min-width:90px"></th></tr></thead><tbody>`+
   (rows.length?rows.map(([k,s])=>{const w=50*Math.abs(s.net)/mx; return `<tr><td>${esc(k)}</td><td class=n>${s.n}</td><td class=n>${P(s.wr)}</td><td class="n ${C(s.net)}"><b>${M(s.net,U)}</b></td><td class="n ${C(s.exp)}">${M(s.exp,U)}</td><td class=n>${F(s.pf)}</td><td><div class="pf-bar"><i style="left:${s.net>=0?50:50-w}%;width:${w}%;background:${s.net>=0?"#00c805":"#ff4d4f"}"></i></div></td></tr>`;}).join(""):`<tr><td colspan=7 class=d>No trades.</td></tr>`)+`</tbody></table></div></div>`;
}
function bottom(el,o){
  const S=build(o.trades||[],o), U=o.unit, T=S.T;
  let h=`<div class="pf">`;
  // months
  const mm={}; for(const t of T){ (mm[t.day.slice(0,7)]??=[]).push(t); } let run=0;
  const mrows=Object.keys(mm).sort().map(k=>{const s=stats(mm[k]); run+=s.net; return {k,s,run};});
  const mlabel=k=>new Date(k+"-15T12:00:00Z").toLocaleString("en-US",{month:"short",year:"numeric",timeZone:"UTC"});
  h+=`<div class="pf-2"><div class="pf-box"><div class="pf-bh"><h2>PnL by month</h2><span class="m">running total = all-time to that month</span></div><div class="pf-bb np tw"><table><thead><tr><th>Month</th><th class=n>Trades</th><th class=n>Win %</th><th class=n>Net</th>${o.start&&U!=="R"?"<th class=n>Return</th>":""}<th class=n>Running total</th></tr></thead><tbody>`+
    mrows.slice().reverse().map(r=>`<tr><td>${mlabel(r.k)}</td><td class=n>${r.s.n}</td><td class=n>${P(r.s.wr)}</td><td class="n ${C(r.s.net)}"><b>${M(r.s.net,U)}</b></td>${o.start&&U!=="R"?`<td class="n ${C(r.s.net)}">${(100*r.s.net/(o.start+r.run-r.s.net)).toFixed(2)}%</td>`:""}<td class="n ${C(r.run)}">${M(r.run,U)}</td></tr>`).join("")+
    `<tr class=tot><td>All-time</td><td class=n>${S.n}</td><td class=n>${P(S.wr)}</td><td class="n ${C(S.net)}">${M(S.net,U)}</td>${o.start&&U!=="R"?`<td class="n ${C(S.net)}">${(100*S.net/o.start).toFixed(2)}%</td>`:""}<td class="n ${C(S.net)}">${M(S.net,U)}</td></tr></tbody></table></div></div>`;
  h+=btable("Long vs short",grp(T,t=>t.side==="long"?"Long":"Short",["Long","Short"]),U)+`</div>`;
  h+=`<div class="pf-2">`+btable("By symbol",grp(T,t=>(t.coin||"").replace(/^xyz:/,"")),U,"sorted by net")+btable("By weekday (entry, ET)",grp(T,t=>t.open_et?WD[wdOf(t.open_et.slice(0,10))]:null,WD),U)+`</div>`;
  h+=`<div class="pf-2">`+btable("By session (entry time, ET)",grp(T,t=>sessOf(t.open_et),SESS.map(x=>x[0])),U)+(o.extraTable?btable(o.extraTable.title,grp(T,o.extraTable.key,o.extraTable.order),U,o.extraTable.sub):`<div></div>`)+`</div>`;
  // trade list
  if(o.noList){ if(o.note) h+=`<div class="pf-note">${o.note}</div>`; el.innerHTML=h+`</div>`; return S; }
  const L=T.slice().reverse(), lim=el.__all?L.length:25;
  h+=`<div class="pf-box"><div class="pf-bh"><h2>Trade log · all-time</h2><span class="m">${T.length} closed · newest first</span></div><div class="pf-bb np tw"><table><thead><tr><th>Closed ET</th><th>Symbol</th><th>Side</th>${o.listExtra?`<th>${o.listExtra.h}</th>`:""}<th class=n>Net</th><th class=n>R</th><th class=n>Fees</th><th>Result</th>${L.some(t=>t.href)?"<th></th>":""}</tr></thead><tbody>`+
    L.slice(0,lim).map(t=>`<tr><td class="d" style="font-family:monospace">${esc((t.close_et||t.day).slice(0,16))}</td><td>${esc((t.coin||"").replace(/^xyz:/,""))}</td><td class="${t.side==="long"?"p":"q"}">${t.side==="long"?"LONG":"SHORT"}</td>${o.listExtra?`<td>${o.listExtra.f(t)}</td>`:""}<td class="n ${C(t.v)}"><b>${M(t.v,U)}</b></td><td class="n ${C(t.r)}">${R(t.r)}</td><td class="n d">${t.fees!=null?M(-t.fees,U):"—"}</td><td><span class="pf-st ${t.v>0.005?"ok":t.v<-0.005?"bad":""}">${t.v>0.005?"WIN":t.v<-0.005?"LOSS":"BE"}</span></td>${L.some(x=>x.href)?`<td>${t.href?`<a href="${esc(t.href)}" target="_blank">journal ↗</a>`:""}</td>`:""}</tr>`).join("")+`</tbody></table></div>${L.length>lim?`<button class="pf-more" data-pfmore>Show all ${L.length} trades ▾</button>`:""}</div>`;
  if(o.note) h+=`<div class="pf-note">${o.note}</div>`;
  h+=`</div>`; el.innerHTML=h;
  const b=el.querySelector("[data-pfmore]"); if(b) b.onclick=()=>{el.__all=true; bottom(el,o);};
  return S;
}
window.PERF={top,bottom,stats:build,money:M};
})();
