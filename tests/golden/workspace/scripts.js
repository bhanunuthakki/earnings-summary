
window.livingGrid = window.livingGrid || function () {
  return {
    q: '', sortKey: '', sortType: 'num', sortDir: -1, total: 0, shown: 0, _rows: [],
    init() {
      var body = this.$root.querySelector('tbody');
      this._rows = Array.prototype.slice.call(body ? body.querySelectorAll('tr') : []);
      this.total = this._rows.length;
      this.shown = this.total;
    },
    arrow: function (key) {
      if (this.sortKey !== key) return '';
      return this.sortDir < 0 ? ' ▾' : ' ▴';
    },
    ariaSort: function (key) {
      if (this.sortKey !== key) return 'none';
      return this.sortDir < 0 ? 'descending' : 'ascending';
    },
    sortBy: function (key, type) {
      if (this.sortKey === key) {
        this.sortDir = -this.sortDir;
      } else {
        this.sortKey = key;
        this.sortType = type || 'num';
        this.sortDir = (type === 'text') ? 1 : -1;
      }
      this.render();
    },
    render: function () {
      var q = this.q.trim().toLowerCase();
      var shown = 0;
      this._rows.forEach(function (tr) {
        var hit = !q || (tr.getAttribute('data-text') || '').indexOf(q) !== -1;
        tr.classList.toggle('lg-hide', !hit);
        if (hit) shown++;
      });
      this.shown = shown;
      if (!this.sortKey) return;
      var key = this.sortKey, type = this.sortType, dir = this.sortDir;
      var body = this.$root.querySelector('tbody');
      if (!body) return;
      this._rows.slice().sort(function (a, b) {
        var av = a.getAttribute('data-' + key);
        var bv = b.getAttribute('data-' + key);
        if (type === 'text') {
          av = (av || '').toLowerCase(); bv = (bv || '').toLowerCase();
          return av < bv ? -dir : (av > bv ? dir : 0);
        }
        var am = (av === null || av === ''), bm = (bv === null || bv === '');
        if (am && bm) return 0;
        if (am) return 1;
        if (bm) return -1;
        return (parseFloat(av) - parseFloat(bv)) * dir;
      }).forEach(function (tr) { body.appendChild(tr); });
    },
  };
};

(()=>{var rt=!1,nt=!1,U=[],it=-1;function qt(e){Cn(e)}function Cn(e){U.includes(e)||U.push(e),Tn()}function Ee(e){let t=U.indexOf(e);t!==-1&&t>it&&U.splice(t,1)}function Tn(){!nt&&!rt&&(rt=!0,queueMicrotask(Rn))}function Rn(){rt=!1,nt=!0;for(let e=0;e<U.length;e++)U[e](),it=e;U.length=0,it=-1,nt=!1}var R,D,L,st,ot=!0;function Ut(e){ot=!1,e(),ot=!0}function Wt(e){R=e.reactive,L=e.release,D=t=>e.effect(t,{scheduler:r=>{ot?qt(r):r()}}),st=e.raw}function at(e){D=e}function Gt(e){let t=()=>{};return[n=>{let i=D(n);return e._x_effects||(e._x_effects=new Set,e._x_runEffects=()=>{e._x_effects.forEach(o=>o())}),e._x_effects.add(i),t=()=>{i!==void 0&&(e._x_effects.delete(i),L(i))},i},()=>{t()}]}function ve(e,t){let r=!0,n,i=D(()=>{let o=e();JSON.stringify(o),r?n=o:queueMicrotask(()=>{t(o,n),n=o}),r=!1});return()=>L(i)}var Jt=[],Yt=[],Xt=[];function Zt(e){Xt.push(e)}function ee(e,t){typeof t=="function"?(e._x_cleanups||(e._x_cleanups=[]),e._x_cleanups.push(t)):(t=e,Yt.push(t))}function Ae(e){Jt.push(e)}function Oe(e,t,r){e._x_attributeCleanups||(e._x_attributeCleanups={}),e._x_attributeCleanups[t]||(e._x_attributeCleanups[t]=[]),e._x_attributeCleanups[t].push(r)}function ct(e,t){e._x_attributeCleanups&&Object.entries(e._x_attributeCleanups).forEach(([r,n])=>{(t===void 0||t.includes(r))&&(n.forEach(i=>i()),delete e._x_attributeCleanups[r])})}function Qt(e){if(e._x_cleanups)for(;e._x_cleanups.length;)e._x_cleanups.pop()()}var lt=new MutationObserver(pt),ut=!1;function le(){lt.observe(document,{subtree:!0,childList:!0,attributes:!0,attributeOldValue:!0}),ut=!0}function ft(){Mn(),lt.disconnect(),ut=!1}var ce=[];function Mn(){let e=lt.takeRecords();ce.push(()=>e.length>0&&pt(e));let t=ce.length;queueMicrotask(()=>{if(ce.length===t)for(;ce.length>0;)ce.shift()()})}function _(e){if(!ut)return e();ft();let t=e();return le(),t}var dt=!1,Se=[];function er(){dt=!0}function tr(){dt=!1,pt(Se),Se=[]}function pt(e){if(dt){Se=Se.concat(e);return}let t=new Set,r=new Set,n=new Map,i=new Map;for(let o=0;o<e.length;o++)if(!e[o].target._x_ignoreMutationObserver&&(e[o].type==="childList"&&(e[o].addedNodes.forEach(s=>s.nodeType===1&&t.add(s)),e[o].removedNodes.forEach(s=>s.nodeType===1&&r.add(s))),e[o].type==="attributes")){let s=e[o].target,a=e[o].attributeName,c=e[o].oldValue,l=()=>{n.has(s)||n.set(s,[]),n.get(s).push({name:a,value:s.getAttribute(a)})},u=()=>{i.has(s)||i.set(s,[]),i.get(s).push(a)};s.hasAttribute(a)&&c===null?l():s.hasAttribute(a)?(u(),l()):u()}i.forEach((o,s)=>{ct(s,o)}),n.forEach((o,s)=>{Jt.forEach(a=>a(s,o))});for(let o of r)t.has(o)||Yt.forEach(s=>s(o));t.forEach(o=>{o._x_ignoreSelf=!0,o._x_ignore=!0});for(let o of t)r.has(o)||o.isConnected&&(delete o._x_ignoreSelf,delete o._x_ignore,Xt.forEach(s=>s(o)),o._x_ignore=!0,o._x_ignoreSelf=!0);t.forEach(o=>{delete o._x_ignoreSelf,delete o._x_ignore}),t=null,r=null,n=null,i=null}function Ce(e){return F(j(e))}function P(e,t,r){return e._x_dataStack=[t,...j(r||e)],()=>{e._x_dataStack=e._x_dataStack.filter(n=>n!==t)}}function j(e){return e._x_dataStack?e._x_dataStack:typeof ShadowRoot=="function"&&e instanceof ShadowRoot?j(e.host):e.parentNode?j(e.parentNode):[]}function F(e){return new Proxy({objects:e},Nn)}var Nn={ownKeys({objects:e}){return Array.from(new Set(e.flatMap(t=>Object.keys(t))))},has({objects:e},t){return t==Symbol.unscopables?!1:e.some(r=>Object.prototype.hasOwnProperty.call(r,t)||Reflect.has(r,t))},get({objects:e},t,r){return t=="toJSON"?Dn:Reflect.get(e.find(n=>Reflect.has(n,t))||{},t,r)},set({objects:e},t,r,n){let i=e.find(s=>Object.prototype.hasOwnProperty.call(s,t))||e[e.length-1],o=Object.getOwnPropertyDescriptor(i,t);return o?.set&&o?.get?o.set.call(n,r)||!0:Reflect.set(i,t,r)}};function Dn(){return Reflect.ownKeys(this).reduce((t,r)=>(t[r]=Reflect.get(this,r),t),{})}function Te(e){let t=n=>typeof n=="object"&&!Array.isArray(n)&&n!==null,r=(n,i="")=>{Object.entries(Object.getOwnPropertyDescriptors(n)).forEach(([o,{value:s,enumerable:a}])=>{if(a===!1||s===void 0||typeof s=="object"&&s!==null&&s.__v_skip)return;let c=i===""?o:`${i}.${o}`;typeof s=="object"&&s!==null&&s._x_interceptor?n[o]=s.initialize(e,c,o):t(s)&&s!==n&&!(s instanceof Element)&&r(s,c)})};return r(e)}function Re(e,t=()=>{}){let r={initialValue:void 0,_x_interceptor:!0,initialize(n,i,o){return e(this.initialValue,()=>Pn(n,i),s=>mt(n,i,s),i,o)}};return t(r),n=>{if(typeof n=="object"&&n!==null&&n._x_interceptor){let i=r.initialize.bind(r);r.initialize=(o,s,a)=>{let c=n.initialize(o,s,a);return r.initialValue=c,i(o,s,a)}}else r.initialValue=n;return r}}function Pn(e,t){return t.split(".").reduce((r,n)=>r[n],e)}function mt(e,t,r){if(typeof t=="string"&&(t=t.split(".")),t.length===1)e[t[0]]=r;else{if(t.length===0)throw error;return e[t[0]]||(e[t[0]]={}),mt(e[t[0]],t.slice(1),r)}}var rr={};function y(e,t){rr[e]=t}function ue(e,t){return Object.entries(rr).forEach(([r,n])=>{let i=null;function o(){if(i)return i;{let[s,a]=_t(t);return i={interceptor:Re,...s},ee(t,a),i}}Object.defineProperty(e,`$${r}`,{get(){return n(t,o())},enumerable:!1})}),e}function nr(e,t,r,...n){try{return r(...n)}catch(i){te(i,e,t)}}function te(e,t,r=void 0){e=Object.assign(e??{message:"No error message given."},{el:t,expression:r}),console.warn(`Alpine Expression Error: ${e.message}

${r?'Expression: "'+r+`"

`:""}`,t),setTimeout(()=>{throw e},0)}var Me=!0;function De(e){let t=Me;Me=!1;let r=e();return Me=t,r}function M(e,t,r={}){let n;return x(e,t)(i=>n=i,r),n}function x(...e){return ir(...e)}var ir=gt;function or(e){ir=e}function gt(e,t){let r={};ue(r,e);let n=[r,...j(e)],i=typeof t=="function"?In(n,t):Ln(n,t,e);return nr.bind(null,e,t,i)}function In(e,t){return(r=()=>{},{scope:n={},params:i=[]}={})=>{let o=t.apply(F([n,...e]),i);Ne(r,o)}}var ht={};function kn(e,t){if(ht[e])return ht[e];let r=Object.getPrototypeOf(async function(){}).constructor,n=/^[\n\s]*if.*\(.*\)/.test(e.trim())||/^(let|const)\s/.test(e.trim())?`(async()=>{ ${e} })()`:e,o=(()=>{try{let s=new r(["__self","scope"],`with (scope) { __self.result = ${n} }; __self.finished = true; return __self.result;`);return Object.defineProperty(s,"name",{value:`[Alpine] ${e}`}),s}catch(s){return te(s,t,e),Promise.resolve()}})();return ht[e]=o,o}function Ln(e,t,r){let n=kn(t,r);return(i=()=>{},{scope:o={},params:s=[]}={})=>{n.result=void 0,n.finished=!1;let a=F([o,...e]);if(typeof n=="function"){let c=n(n,a).catch(l=>te(l,r,t));n.finished?(Ne(i,n.result,a,s,r),n.result=void 0):c.then(l=>{Ne(i,l,a,s,r)}).catch(l=>te(l,r,t)).finally(()=>n.result=void 0)}}}function Ne(e,t,r,n,i){if(Me&&typeof t=="function"){let o=t.apply(r,n);o instanceof Promise?o.then(s=>Ne(e,s,r,n)).catch(s=>te(s,i,t)):e(o)}else typeof t=="object"&&t instanceof Promise?t.then(o=>e(o)):e(t)}var bt="x-";function C(e=""){return bt+e}function sr(e){bt=e}var Pe={};function d(e,t){return Pe[e]=t,{before(r){if(!Pe[r]){console.warn(String.raw`Cannot find directive \`${r}\`. \`${e}\` will use the default order of execution`);return}let n=W.indexOf(r);W.splice(n>=0?n:W.indexOf("DEFAULT"),0,e)}}}function ar(e){return Object.keys(Pe).includes(e)}function de(e,t,r){if(t=Array.from(t),e._x_virtualDirectives){let o=Object.entries(e._x_virtualDirectives).map(([a,c])=>({name:a,value:c})),s=wt(o);o=o.map(a=>s.find(c=>c.name===a.name)?{name:`x-bind:${a.name}`,value:`"${a.value}"`}:a),t=t.concat(o)}let n={};return t.map(ur((o,s)=>n[o]=s)).filter(dr).map(jn(n,r)).sort(Fn).map(o=>$n(e,o))}function wt(e){return Array.from(e).map(ur()).filter(t=>!dr(t))}var xt=!1,fe=new Map,cr=Symbol();function lr(e){xt=!0;let t=Symbol();cr=t,fe.set(t,[]);let r=()=>{for(;fe.get(t).length;)fe.get(t).shift()();fe.delete(t)},n=()=>{xt=!1,r()};e(r),n()}function _t(e){let t=[],r=a=>t.push(a),[n,i]=Gt(e);return t.push(i),[{Alpine:B,effect:n,cleanup:r,evaluateLater:x.bind(x,e),evaluate:M.bind(M,e)},()=>t.forEach(a=>a())]}function $n(e,t){let r=()=>{},n=Pe[t.type]||r,[i,o]=_t(e);Oe(e,t.original,o);let s=()=>{e._x_ignore||e._x_ignoreSelf||(n.inline&&n.inline(e,t,i),n=n.bind(n,e,t,i),xt?fe.get(cr).push(n):n())};return s.runCleanups=o,s}var Ie=(e,t)=>({name:r,value:n})=>(r.startsWith(e)&&(r=r.replace(e,t)),{name:r,value:n}),ke=e=>e;function ur(e=()=>{}){return({name:t,value:r})=>{let{name:n,value:i}=fr.reduce((o,s)=>s(o),{name:t,value:r});return n!==t&&e(n,t),{name:n,value:i}}}var fr=[];function re(e){fr.push(e)}function dr({name:e}){return pr().test(e)}var pr=()=>new RegExp(`^${bt}([^:^.]+)\\b`);function jn(e,t){return({name:r,value:n})=>{let i=r.match(pr()),o=r.match(/:([a-zA-Z0-9\-_:]+)/),s=r.match(/\.[^.\]]+(?=[^\]]*$)/g)||[],a=t||e[r]||r;return{type:i?i[1]:null,value:o?o[1]:null,modifiers:s.map(c=>c.replace(".","")),expression:n,original:a}}}var yt="DEFAULT",W=["ignore","ref","data","id","anchor","bind","init","for","model","modelable","transition","show","if",yt,"teleport"];function Fn(e,t){let r=W.indexOf(e.type)===-1?yt:e.type,n=W.indexOf(t.type)===-1?yt:t.type;return W.indexOf(r)-W.indexOf(n)}function G(e,t,r={}){e.dispatchEvent(new CustomEvent(t,{detail:r,bubbles:!0,composed:!0,cancelable:!0}))}function T(e,t){if(typeof ShadowRoot=="function"&&e instanceof ShadowRoot){Array.from(e.children).forEach(i=>T(i,t));return}let r=!1;if(t(e,()=>r=!0),r)return;let n=e.firstElementChild;for(;n;)T(n,t,!1),n=n.nextElementSibling}function E(e,...t){console.warn(`Alpine Warning: ${e}`,...t)}var mr=!1;function _r(){mr&&E("Alpine has already been initialized on this page. Calling Alpine.start() more than once can cause problems."),mr=!0,document.body||E("Unable to initialize. Trying to load Alpine before `<body>` is available. Did you forget to add `defer` in Alpine's `<script>` tag?"),G(document,"alpine:init"),G(document,"alpine:initializing"),le(),Zt(t=>S(t,T)),ee(t=>vt(t)),Ae((t,r)=>{de(t,r).forEach(n=>n())});let e=t=>!J(t.parentElement,!0);Array.from(document.querySelectorAll(xr().join(","))).filter(e).forEach(t=>{S(t)}),G(document,"alpine:initialized"),setTimeout(()=>{Bn()})}var Et=[],hr=[];function gr(){return Et.map(e=>e())}function xr(){return Et.concat(hr).map(e=>e())}function Le(e){Et.push(e)}function $e(e){hr.push(e)}function J(e,t=!1){return z(e,r=>{if((t?xr():gr()).some(i=>r.matches(i)))return!0})}function z(e,t){if(e){if(t(e))return e;if(e._x_teleportBack&&(e=e._x_teleportBack),!!e.parentElement)return z(e.parentElement,t)}}function yr(e){return gr().some(t=>e.matches(t))}var br=[];function wr(e){br.push(e)}function S(e,t=T,r=()=>{}){lr(()=>{t(e,(n,i)=>{r(n,i),br.forEach(o=>o(n,i)),de(n,n.attributes).forEach(o=>o()),n._x_ignore&&i()})})}function vt(e,t=T){t(e,r=>{ct(r),Qt(r)})}function Bn(){[["ui","dialog",["[x-dialog], [x-popover]"]],["anchor","anchor",["[x-anchor]"]],["sort","sort",["[x-sort]"]]].forEach(([t,r,n])=>{ar(r)||n.some(i=>{if(document.querySelector(i))return E(`found "${i}", but missing ${t} plugin`),!0})})}var St=[],At=!1;function ne(e=()=>{}){return queueMicrotask(()=>{At||setTimeout(()=>{je()})}),new Promise(t=>{St.push(()=>{e(),t()})})}function je(){for(At=!1;St.length;)St.shift()()}function Er(){At=!0}function pe(e,t){return Array.isArray(t)?vr(e,t.join(" ")):typeof t=="object"&&t!==null?zn(e,t):typeof t=="function"?pe(e,t()):vr(e,t)}function vr(e,t){let r=o=>o.split(" ").filter(Boolean),n=o=>o.split(" ").filter(s=>!e.classList.contains(s)).filter(Boolean),i=o=>(e.classList.add(...o),()=>{e.classList.remove(...o)});return t=t===!0?t="":t||"",i(n(t))}function zn(e,t){let r=a=>a.split(" ").filter(Boolean),n=Object.entries(t).flatMap(([a,c])=>c?r(a):!1).filter(Boolean),i=Object.entries(t).flatMap(([a,c])=>c?!1:r(a)).filter(Boolean),o=[],s=[];return i.forEach(a=>{e.classList.contains(a)&&(e.classList.remove(a),s.push(a))}),n.forEach(a=>{e.classList.contains(a)||(e.classList.add(a),o.push(a))}),()=>{s.forEach(a=>e.classList.add(a)),o.forEach(a=>e.classList.remove(a))}}function Y(e,t){return typeof t=="object"&&t!==null?Kn(e,t):Hn(e,t)}function Kn(e,t){let r={};return Object.entries(t).forEach(([n,i])=>{r[n]=e.style[n],n.startsWith("--")||(n=Vn(n)),e.style.setProperty(n,i)}),setTimeout(()=>{e.style.length===0&&e.removeAttribute("style")}),()=>{Y(e,r)}}function Hn(e,t){let r=e.getAttribute("style",t);return e.setAttribute("style",t),()=>{e.setAttribute("style",r||"")}}function Vn(e){return e.replace(/([a-z])([A-Z])/g,"$1-$2").toLowerCase()}function me(e,t=()=>{}){let r=!1;return function(){r?t.apply(this,arguments):(r=!0,e.apply(this,arguments))}}d("transition",(e,{value:t,modifiers:r,expression:n},{evaluate:i})=>{typeof n=="function"&&(n=i(n)),n!==!1&&(!n||typeof n=="boolean"?Un(e,r,t):qn(e,n,t))});function qn(e,t,r){Sr(e,pe,""),{enter:i=>{e._x_transition.enter.during=i},"enter-start":i=>{e._x_transition.enter.start=i},"enter-end":i=>{e._x_transition.enter.end=i},leave:i=>{e._x_transition.leave.during=i},"leave-start":i=>{e._x_transition.leave.start=i},"leave-end":i=>{e._x_transition.leave.end=i}}[r](t)}function Un(e,t,r){Sr(e,Y);let n=!t.includes("in")&&!t.includes("out")&&!r,i=n||t.includes("in")||["enter"].includes(r),o=n||t.includes("out")||["leave"].includes(r);t.includes("in")&&!n&&(t=t.filter((g,b)=>b<t.indexOf("out"))),t.includes("out")&&!n&&(t=t.filter((g,b)=>b>t.indexOf("out")));let s=!t.includes("opacity")&&!t.includes("scale"),a=s||t.includes("opacity"),c=s||t.includes("scale"),l=a?0:1,u=c?_e(t,"scale",95)/100:1,p=_e(t,"delay",0)/1e3,m=_e(t,"origin","center"),w="opacity, transform",$=_e(t,"duration",150)/1e3,we=_e(t,"duration",75)/1e3,f="cubic-bezier(0.4, 0.0, 0.2, 1)";i&&(e._x_transition.enter.during={transformOrigin:m,transitionDelay:`${p}s`,transitionProperty:w,transitionDuration:`${$}s`,transitionTimingFunction:f},e._x_transition.enter.start={opacity:l,transform:`scale(${u})`},e._x_transition.enter.end={opacity:1,transform:"scale(1)"}),o&&(e._x_transition.leave.during={transformOrigin:m,transitionDelay:`${p}s`,transitionProperty:w,transitionDuration:`${we}s`,transitionTimingFunction:f},e._x_transition.leave.start={opacity:1,transform:"scale(1)"},e._x_transition.leave.end={opacity:l,transform:`scale(${u})`})}function Sr(e,t,r={}){e._x_transition||(e._x_transition={enter:{during:r,start:r,end:r},leave:{during:r,start:r,end:r},in(n=()=>{},i=()=>{}){Fe(e,t,{during:this.enter.during,start:this.enter.start,end:this.enter.end},n,i)},out(n=()=>{},i=()=>{}){Fe(e,t,{during:this.leave.during,start:this.leave.start,end:this.leave.end},n,i)}})}window.Element.prototype._x_toggleAndCascadeWithTransitions=function(e,t,r,n){let i=document.visibilityState==="visible"?requestAnimationFrame:setTimeout,o=()=>i(r);if(t){e._x_transition&&(e._x_transition.enter||e._x_transition.leave)?e._x_transition.enter&&(Object.entries(e._x_transition.enter.during).length||Object.entries(e._x_transition.enter.start).length||Object.entries(e._x_transition.enter.end).length)?e._x_transition.in(r):o():e._x_transition?e._x_transition.in(r):o();return}e._x_hidePromise=e._x_transition?new Promise((s,a)=>{e._x_transition.out(()=>{},()=>s(n)),e._x_transitioning&&e._x_transitioning.beforeCancel(()=>a({isFromCancelledTransition:!0}))}):Promise.resolve(n),queueMicrotask(()=>{let s=Ar(e);s?(s._x_hideChildren||(s._x_hideChildren=[]),s._x_hideChildren.push(e)):i(()=>{let a=c=>{let l=Promise.all([c._x_hidePromise,...(c._x_hideChildren||[]).map(a)]).then(([u])=>u?.());return delete c._x_hidePromise,delete c._x_hideChildren,l};a(e).catch(c=>{if(!c.isFromCancelledTransition)throw c})})})};function Ar(e){let t=e.parentNode;if(t)return t._x_hidePromise?t:Ar(t)}function Fe(e,t,{during:r,start:n,end:i}={},o=()=>{},s=()=>{}){if(e._x_transitioning&&e._x_transitioning.cancel(),Object.keys(r).length===0&&Object.keys(n).length===0&&Object.keys(i).length===0){o(),s();return}let a,c,l;Wn(e,{start(){a=t(e,n)},during(){c=t(e,r)},before:o,end(){a(),l=t(e,i)},after:s,cleanup(){c(),l()}})}function Wn(e,t){let r,n,i,o=me(()=>{_(()=>{r=!0,n||t.before(),i||(t.end(),je()),t.after(),e.isConnected&&t.cleanup(),delete e._x_transitioning})});e._x_transitioning={beforeCancels:[],beforeCancel(s){this.beforeCancels.push(s)},cancel:me(function(){for(;this.beforeCancels.length;)this.beforeCancels.shift()();o()}),finish:o},_(()=>{t.start(),t.during()}),Er(),requestAnimationFrame(()=>{if(r)return;let s=Number(getComputedStyle(e).transitionDuration.replace(/,.*/,"").replace("s",""))*1e3,a=Number(getComputedStyle(e).transitionDelay.replace(/,.*/,"").replace("s",""))*1e3;s===0&&(s=Number(getComputedStyle(e).animationDuration.replace("s",""))*1e3),_(()=>{t.before()}),n=!0,requestAnimationFrame(()=>{r||(_(()=>{t.end()}),je(),setTimeout(e._x_transitioning.finish,s+a),i=!0)})})}function _e(e,t,r){if(e.indexOf(t)===-1)return r;let n=e[e.indexOf(t)+1];if(!n||t==="scale"&&isNaN(n))return r;if(t==="duration"||t==="delay"){let i=n.match(/([0-9]+)ms/);if(i)return i[1]}return t==="origin"&&["top","right","left","center","bottom"].includes(e[e.indexOf(t)+2])?[n,e[e.indexOf(t)+2]].join(" "):n}var I=!1;function A(e,t=()=>{}){return(...r)=>I?t(...r):e(...r)}function Or(e){return(...t)=>I&&e(...t)}var Cr=[];function K(e){Cr.push(e)}function Tr(e,t){Cr.forEach(r=>r(e,t)),I=!0,Mr(()=>{S(t,(r,n)=>{n(r,()=>{})})}),I=!1}var Be=!1;function Rr(e,t){t._x_dataStack||(t._x_dataStack=e._x_dataStack),I=!0,Be=!0,Mr(()=>{Gn(t)}),I=!1,Be=!1}function Gn(e){let t=!1;S(e,(n,i)=>{T(n,(o,s)=>{if(t&&yr(o))return s();t=!0,i(o,s)})})}function Mr(e){let t=D;at((r,n)=>{let i=t(r);return L(i),()=>{}}),e(),at(t)}function he(e,t,r,n=[]){switch(e._x_bindings||(e._x_bindings=R({})),e._x_bindings[t]=r,t=n.includes("camel")?ri(t):t,t){case"value":Jn(e,r);break;case"style":Xn(e,r);break;case"class":Yn(e,r);break;case"selected":case"checked":Zn(e,t,r);break;default:Dr(e,t,r);break}}function Jn(e,t){if(e.type==="radio")e.attributes.value===void 0&&(e.value=t),window.fromModel&&(typeof t=="boolean"?e.checked=ge(e.value)===t:e.checked=Nr(e.value,t));else if(e.type==="checkbox")Number.isInteger(t)?e.value=t:!Array.isArray(t)&&typeof t!="boolean"&&![null,void 0].includes(t)?e.value=String(t):Array.isArray(t)?e.checked=t.some(r=>Nr(r,e.value)):e.checked=!!t;else if(e.tagName==="SELECT")ti(e,t);else{if(e.value===t)return;e.value=t===void 0?"":t}}function Yn(e,t){e._x_undoAddedClasses&&e._x_undoAddedClasses(),e._x_undoAddedClasses=pe(e,t)}function Xn(e,t){e._x_undoAddedStyles&&e._x_undoAddedStyles(),e._x_undoAddedStyles=Y(e,t)}function Zn(e,t,r){Dr(e,t,r),ei(e,t,r)}function Dr(e,t,r){[null,void 0,!1].includes(r)&&ni(t)?e.removeAttribute(t):(Pr(t)&&(r=t),Qn(e,t,r))}function Qn(e,t,r){e.getAttribute(t)!=r&&e.setAttribute(t,r)}function ei(e,t,r){e[t]!==r&&(e[t]=r)}function ti(e,t){let r=[].concat(t).map(n=>n+"");Array.from(e.options).forEach(n=>{n.selected=r.includes(n.value)})}function ri(e){return e.toLowerCase().replace(/-(\w)/g,(t,r)=>r.toUpperCase())}function Nr(e,t){return e==t}function ge(e){return[1,"1","true","on","yes",!0].includes(e)?!0:[0,"0","false","off","no",!1].includes(e)?!1:e?Boolean(e):null}function Pr(e){return["disabled","checked","required","readonly","open","selected","autofocus","itemscope","multiple","novalidate","allowfullscreen","allowpaymentrequest","formnovalidate","autoplay","controls","loop","muted","playsinline","default","ismap","reversed","async","defer","nomodule"].includes(e)}function ni(e){return!["aria-pressed","aria-checked","aria-expanded","aria-selected"].includes(e)}function Ir(e,t,r){return e._x_bindings&&e._x_bindings[t]!==void 0?e._x_bindings[t]:Lr(e,t,r)}function kr(e,t,r,n=!0){if(e._x_bindings&&e._x_bindings[t]!==void 0)return e._x_bindings[t];if(e._x_inlineBindings&&e._x_inlineBindings[t]!==void 0){let i=e._x_inlineBindings[t];return i.extract=n,De(()=>M(e,i.expression))}return Lr(e,t,r)}function Lr(e,t,r){let n=e.getAttribute(t);return n===null?typeof r=="function"?r():r:n===""?!0:Pr(t)?!![t,"true"].includes(n):n}function ze(e,t){var r;return function(){var n=this,i=arguments,o=function(){r=null,e.apply(n,i)};clearTimeout(r),r=setTimeout(o,t)}}function Ke(e,t){let r;return function(){let n=this,i=arguments;r||(e.apply(n,i),r=!0,setTimeout(()=>r=!1,t))}}function He({get:e,set:t},{get:r,set:n}){let i=!0,o,s,a=D(()=>{let c=e(),l=r();if(i)n(Ot(c)),i=!1;else{let u=JSON.stringify(c),p=JSON.stringify(l);u!==o?n(Ot(c)):u!==p&&t(Ot(l))}o=JSON.stringify(e()),s=JSON.stringify(r())});return()=>{L(a)}}function Ot(e){return typeof e=="object"?JSON.parse(JSON.stringify(e)):e}function $r(e){(Array.isArray(e)?e:[e]).forEach(r=>r(B))}var X={},jr=!1;function Fr(e,t){if(jr||(X=R(X),jr=!0),t===void 0)return X[e];X[e]=t,typeof t=="object"&&t!==null&&t.hasOwnProperty("init")&&typeof t.init=="function"&&X[e].init(),Te(X[e])}function Br(){return X}var zr={};function Kr(e,t){let r=typeof t!="function"?()=>t:t;return e instanceof Element?Ct(e,r()):(zr[e]=r,()=>{})}function Hr(e){return Object.entries(zr).forEach(([t,r])=>{Object.defineProperty(e,t,{get(){return(...n)=>r(...n)}})}),e}function Ct(e,t,r){let n=[];for(;n.length;)n.pop()();let i=Object.entries(t).map(([s,a])=>({name:s,value:a})),o=wt(i);return i=i.map(s=>o.find(a=>a.name===s.name)?{name:`x-bind:${s.name}`,value:`"${s.value}"`}:s),de(e,i,r).map(s=>{n.push(s.runCleanups),s()}),()=>{for(;n.length;)n.pop()()}}var Vr={};function qr(e,t){Vr[e]=t}function Ur(e,t){return Object.entries(Vr).forEach(([r,n])=>{Object.defineProperty(e,r,{get(){return(...i)=>n.bind(t)(...i)},enumerable:!1})}),e}var ii={get reactive(){return R},get release(){return L},get effect(){return D},get raw(){return st},version:"3.14.1",flushAndStopDeferringMutations:tr,dontAutoEvaluateFunctions:De,disableEffectScheduling:Ut,startObservingMutations:le,stopObservingMutations:ft,setReactivityEngine:Wt,onAttributeRemoved:Oe,onAttributesAdded:Ae,closestDataStack:j,skipDuringClone:A,onlyDuringClone:Or,addRootSelector:Le,addInitSelector:$e,interceptClone:K,addScopeToNode:P,deferMutations:er,mapAttributes:re,evaluateLater:x,interceptInit:wr,setEvaluator:or,mergeProxies:F,extractProp:kr,findClosest:z,onElRemoved:ee,closestRoot:J,destroyTree:vt,interceptor:Re,transition:Fe,setStyles:Y,mutateDom:_,directive:d,entangle:He,throttle:Ke,debounce:ze,evaluate:M,initTree:S,nextTick:ne,prefixed:C,prefix:sr,plugin:$r,magic:y,store:Fr,start:_r,clone:Rr,cloneNode:Tr,bound:Ir,$data:Ce,watch:ve,walk:T,data:qr,bind:Kr},B=ii;function Tt(e,t){let r=Object.create(null),n=e.split(",");for(let i=0;i<n.length;i++)r[n[i]]=!0;return t?i=>!!r[i.toLowerCase()]:i=>!!r[i]}var oi="itemscope,allowfullscreen,formnovalidate,ismap,nomodule,novalidate,readonly";var Ms=Tt(oi+",async,autofocus,autoplay,controls,default,defer,disabled,hidden,loop,open,required,reversed,scoped,seamless,checked,muted,multiple,selected");var Wr=Object.freeze({}),Ns=Object.freeze([]);var si=Object.prototype.hasOwnProperty,xe=(e,t)=>si.call(e,t),H=Array.isArray,ie=e=>Gr(e)==="[object Map]";var ai=e=>typeof e=="string",Ve=e=>typeof e=="symbol",ye=e=>e!==null&&typeof e=="object";var ci=Object.prototype.toString,Gr=e=>ci.call(e),Rt=e=>Gr(e).slice(8,-1);var qe=e=>ai(e)&&e!=="NaN"&&e[0]!=="-"&&""+parseInt(e,10)===e;var Ue=e=>{let t=Object.create(null);return r=>t[r]||(t[r]=e(r))},li=/-(\w)/g,Ds=Ue(e=>e.replace(li,(t,r)=>r?r.toUpperCase():"")),ui=/\B([A-Z])/g,Ps=Ue(e=>e.replace(ui,"-$1").toLowerCase()),Mt=Ue(e=>e.charAt(0).toUpperCase()+e.slice(1)),Is=Ue(e=>e?`on${Mt(e)}`:""),Nt=(e,t)=>e!==t&&(e===e||t===t);var Dt=new WeakMap,be=[],k,Z=Symbol("iterate"),Pt=Symbol("Map key iterate");function fi(e){return e&&e._isEffect===!0}function en(e,t=Wr){fi(e)&&(e=e.raw);let r=pi(e,t);return t.lazy||r(),r}function tn(e){e.active&&(rn(e),e.options.onStop&&e.options.onStop(),e.active=!1)}var di=0;function pi(e,t){let r=function(){if(!r.active)return e();if(!be.includes(r)){rn(r);try{return _i(),be.push(r),k=r,e()}finally{be.pop(),nn(),k=be[be.length-1]}}};return r.id=di++,r.allowRecurse=!!t.allowRecurse,r._isEffect=!0,r.active=!0,r.raw=e,r.deps=[],r.options=t,r}function rn(e){let{deps:t}=e;if(t.length){for(let r=0;r<t.length;r++)t[r].delete(e);t.length=0}}var oe=!0,kt=[];function mi(){kt.push(oe),oe=!1}function _i(){kt.push(oe),oe=!0}function nn(){let e=kt.pop();oe=e===void 0?!0:e}function N(e,t,r){if(!oe||k===void 0)return;let n=Dt.get(e);n||Dt.set(e,n=new Map);let i=n.get(r);i||n.set(r,i=new Set),i.has(k)||(i.add(k),k.deps.push(i),k.options.onTrack&&k.options.onTrack({effect:k,target:e,type:t,key:r}))}function q(e,t,r,n,i,o){let s=Dt.get(e);if(!s)return;let a=new Set,c=u=>{u&&u.forEach(p=>{(p!==k||p.allowRecurse)&&a.add(p)})};if(t==="clear")s.forEach(c);else if(r==="length"&&H(e))s.forEach((u,p)=>{(p==="length"||p>=n)&&c(u)});else switch(r!==void 0&&c(s.get(r)),t){case"add":H(e)?qe(r)&&c(s.get("length")):(c(s.get(Z)),ie(e)&&c(s.get(Pt)));break;case"delete":H(e)||(c(s.get(Z)),ie(e)&&c(s.get(Pt)));break;case"set":ie(e)&&c(s.get(Z));break}let l=u=>{u.options.onTrigger&&u.options.onTrigger({effect:u,target:e,key:r,type:t,newValue:n,oldValue:i,oldTarget:o}),u.options.scheduler?u.options.scheduler(u):u()};a.forEach(l)}var hi=Tt("__proto__,__v_isRef,__isVue"),on=new Set(Object.getOwnPropertyNames(Symbol).map(e=>Symbol[e]).filter(Ve)),gi=sn();var xi=sn(!0);var Jr=yi();function yi(){let e={};return["includes","indexOf","lastIndexOf"].forEach(t=>{e[t]=function(...r){let n=h(this);for(let o=0,s=this.length;o<s;o++)N(n,"get",o+"");let i=n[t](...r);return i===-1||i===!1?n[t](...r.map(h)):i}}),["push","pop","shift","unshift","splice"].forEach(t=>{e[t]=function(...r){mi();let n=h(this)[t].apply(this,r);return nn(),n}}),e}function sn(e=!1,t=!1){return function(n,i,o){if(i==="__v_isReactive")return!e;if(i==="__v_isReadonly")return e;if(i==="__v_raw"&&o===(e?t?ki:un:t?Ii:ln).get(n))return n;let s=H(n);if(!e&&s&&xe(Jr,i))return Reflect.get(Jr,i,o);let a=Reflect.get(n,i,o);return(Ve(i)?on.has(i):hi(i))||(e||N(n,"get",i),t)?a:It(a)?!s||!qe(i)?a.value:a:ye(a)?e?fn(a):Qe(a):a}}var bi=wi();function wi(e=!1){return function(r,n,i,o){let s=r[n];if(!e&&(i=h(i),s=h(s),!H(r)&&It(s)&&!It(i)))return s.value=i,!0;let a=H(r)&&qe(n)?Number(n)<r.length:xe(r,n),c=Reflect.set(r,n,i,o);return r===h(o)&&(a?Nt(i,s)&&q(r,"set",n,i,s):q(r,"add",n,i)),c}}function Ei(e,t){let r=xe(e,t),n=e[t],i=Reflect.deleteProperty(e,t);return i&&r&&q(e,"delete",t,void 0,n),i}function vi(e,t){let r=Reflect.has(e,t);return(!Ve(t)||!on.has(t))&&N(e,"has",t),r}function Si(e){return N(e,"iterate",H(e)?"length":Z),Reflect.ownKeys(e)}var Ai={get:gi,set:bi,deleteProperty:Ei,has:vi,ownKeys:Si},Oi={get:xi,set(e,t){return console.warn(`Set operation on key "${String(t)}" failed: target is readonly.`,e),!0},deleteProperty(e,t){return console.warn(`Delete operation on key "${String(t)}" failed: target is readonly.`,e),!0}};var Lt=e=>ye(e)?Qe(e):e,$t=e=>ye(e)?fn(e):e,jt=e=>e,Ze=e=>Reflect.getPrototypeOf(e);function We(e,t,r=!1,n=!1){e=e.__v_raw;let i=h(e),o=h(t);t!==o&&!r&&N(i,"get",t),!r&&N(i,"get",o);let{has:s}=Ze(i),a=n?jt:r?$t:Lt;if(s.call(i,t))return a(e.get(t));if(s.call(i,o))return a(e.get(o));e!==i&&e.get(t)}function Ge(e,t=!1){let r=this.__v_raw,n=h(r),i=h(e);return e!==i&&!t&&N(n,"has",e),!t&&N(n,"has",i),e===i?r.has(e):r.has(e)||r.has(i)}function Je(e,t=!1){return e=e.__v_raw,!t&&N(h(e),"iterate",Z),Reflect.get(e,"size",e)}function Yr(e){e=h(e);let t=h(this);return Ze(t).has.call(t,e)||(t.add(e),q(t,"add",e,e)),this}function Xr(e,t){t=h(t);let r=h(this),{has:n,get:i}=Ze(r),o=n.call(r,e);o?cn(r,n,e):(e=h(e),o=n.call(r,e));let s=i.call(r,e);return r.set(e,t),o?Nt(t,s)&&q(r,"set",e,t,s):q(r,"add",e,t),this}function Zr(e){let t=h(this),{has:r,get:n}=Ze(t),i=r.call(t,e);i?cn(t,r,e):(e=h(e),i=r.call(t,e));let o=n?n.call(t,e):void 0,s=t.delete(e);return i&&q(t,"delete",e,void 0,o),s}function Qr(){let e=h(this),t=e.size!==0,r=ie(e)?new Map(e):new Set(e),n=e.clear();return t&&q(e,"clear",void 0,void 0,r),n}function Ye(e,t){return function(n,i){let o=this,s=o.__v_raw,a=h(s),c=t?jt:e?$t:Lt;return!e&&N(a,"iterate",Z),s.forEach((l,u)=>n.call(i,c(l),c(u),o))}}function Xe(e,t,r){return function(...n){let i=this.__v_raw,o=h(i),s=ie(o),a=e==="entries"||e===Symbol.iterator&&s,c=e==="keys"&&s,l=i[e](...n),u=r?jt:t?$t:Lt;return!t&&N(o,"iterate",c?Pt:Z),{next(){let{value:p,done:m}=l.next();return m?{value:p,done:m}:{value:a?[u(p[0]),u(p[1])]:u(p),done:m}},[Symbol.iterator](){return this}}}}function V(e){return function(...t){{let r=t[0]?`on key "${t[0]}" `:"";console.warn(`${Mt(e)} operation ${r}failed: target is readonly.`,h(this))}return e==="delete"?!1:this}}function Ci(){let e={get(o){return We(this,o)},get size(){return Je(this)},has:Ge,add:Yr,set:Xr,delete:Zr,clear:Qr,forEach:Ye(!1,!1)},t={get(o){return We(this,o,!1,!0)},get size(){return Je(this)},has:Ge,add:Yr,set:Xr,delete:Zr,clear:Qr,forEach:Ye(!1,!0)},r={get(o){return We(this,o,!0)},get size(){return Je(this,!0)},has(o){return Ge.call(this,o,!0)},add:V("add"),set:V("set"),delete:V("delete"),clear:V("clear"),forEach:Ye(!0,!1)},n={get(o){return We(this,o,!0,!0)},get size(){return Je(this,!0)},has(o){return Ge.call(this,o,!0)},add:V("add"),set:V("set"),delete:V("delete"),clear:V("clear"),forEach:Ye(!0,!0)};return["keys","values","entries",Symbol.iterator].forEach(o=>{e[o]=Xe(o,!1,!1),r[o]=Xe(o,!0,!1),t[o]=Xe(o,!1,!0),n[o]=Xe(o,!0,!0)}),[e,r,t,n]}var[Ti,Ri,Mi,Ni]=Ci();function an(e,t){let r=t?e?Ni:Mi:e?Ri:Ti;return(n,i,o)=>i==="__v_isReactive"?!e:i==="__v_isReadonly"?e:i==="__v_raw"?n:Reflect.get(xe(r,i)&&i in n?r:n,i,o)}var Di={get:an(!1,!1)};var Pi={get:an(!0,!1)};function cn(e,t,r){let n=h(r);if(n!==r&&t.call(e,n)){let i=Rt(e);console.warn(`Reactive ${i} contains both the raw and reactive versions of the same object${i==="Map"?" as keys":""}, which can lead to inconsistencies. Avoid differentiating between the raw and reactive versions of an object and only use the reactive version if possible.`)}}var ln=new WeakMap,Ii=new WeakMap,un=new WeakMap,ki=new WeakMap;function Li(e){switch(e){case"Object":case"Array":return 1;case"Map":case"Set":case"WeakMap":case"WeakSet":return 2;default:return 0}}function $i(e){return e.__v_skip||!Object.isExtensible(e)?0:Li(Rt(e))}function Qe(e){return e&&e.__v_isReadonly?e:dn(e,!1,Ai,Di,ln)}function fn(e){return dn(e,!0,Oi,Pi,un)}function dn(e,t,r,n,i){if(!ye(e))return console.warn(`value cannot be made reactive: ${String(e)}`),e;if(e.__v_raw&&!(t&&e.__v_isReactive))return e;let o=i.get(e);if(o)return o;let s=$i(e);if(s===0)return e;let a=new Proxy(e,s===2?n:r);return i.set(e,a),a}function h(e){return e&&h(e.__v_raw)||e}function It(e){return Boolean(e&&e.__v_isRef===!0)}y("nextTick",()=>ne);y("dispatch",e=>G.bind(G,e));y("watch",(e,{evaluateLater:t,cleanup:r})=>(n,i)=>{let o=t(n),a=ve(()=>{let c;return o(l=>c=l),c},i);r(a)});y("store",Br);y("data",e=>Ce(e));y("root",e=>J(e));y("refs",e=>(e._x_refs_proxy||(e._x_refs_proxy=F(ji(e))),e._x_refs_proxy));function ji(e){let t=[];return z(e,r=>{r._x_refs&&t.push(r._x_refs)}),t}var Ft={};function Bt(e){return Ft[e]||(Ft[e]=0),++Ft[e]}function pn(e,t){return z(e,r=>{if(r._x_ids&&r._x_ids[t])return!0})}function mn(e,t){e._x_ids||(e._x_ids={}),e._x_ids[t]||(e._x_ids[t]=Bt(t))}y("id",(e,{cleanup:t})=>(r,n=null)=>{let i=`${r}${n?`-${n}`:""}`;return Fi(e,i,t,()=>{let o=pn(e,r),s=o?o._x_ids[r]:Bt(r);return n?`${r}-${s}-${n}`:`${r}-${s}`})});K((e,t)=>{e._x_id&&(t._x_id=e._x_id)});function Fi(e,t,r,n){if(e._x_id||(e._x_id={}),e._x_id[t])return e._x_id[t];let i=n();return e._x_id[t]=i,r(()=>{delete e._x_id[t]}),i}y("el",e=>e);_n("Focus","focus","focus");_n("Persist","persist","persist");function _n(e,t,r){y(t,n=>E(`You can't use [$${t}] without first installing the "${e}" plugin here: https://alpinejs.dev/plugins/${r}`,n))}d("modelable",(e,{expression:t},{effect:r,evaluateLater:n,cleanup:i})=>{let o=n(t),s=()=>{let u;return o(p=>u=p),u},a=n(`${t} = __placeholder`),c=u=>a(()=>{},{scope:{__placeholder:u}}),l=s();c(l),queueMicrotask(()=>{if(!e._x_model)return;e._x_removeModelListeners.default();let u=e._x_model.get,p=e._x_model.set,m=He({get(){return u()},set(w){p(w)}},{get(){return s()},set(w){c(w)}});i(m)})});d("teleport",(e,{modifiers:t,expression:r},{cleanup:n})=>{e.tagName.toLowerCase()!=="template"&&E("x-teleport can only be used on a <template> tag",e);let i=hn(r),o=e.content.cloneNode(!0).firstElementChild;e._x_teleport=o,o._x_teleportBack=e,e.setAttribute("data-teleport-template",!0),o.setAttribute("data-teleport-target",!0),e._x_forwardEvents&&e._x_forwardEvents.forEach(a=>{o.addEventListener(a,c=>{c.stopPropagation(),e.dispatchEvent(new c.constructor(c.type,c))})}),P(o,{},e);let s=(a,c,l)=>{l.includes("prepend")?c.parentNode.insertBefore(a,c):l.includes("append")?c.parentNode.insertBefore(a,c.nextSibling):c.appendChild(a)};_(()=>{s(o,i,t),A(()=>{S(o),o._x_ignore=!0})()}),e._x_teleportPutBack=()=>{let a=hn(r);_(()=>{s(e._x_teleport,a,t)})},n(()=>o.remove())});var Bi=document.createElement("div");function hn(e){let t=A(()=>document.querySelector(e),()=>Bi)();return t||E(`Cannot find x-teleport element for selector: "${e}"`),t}var gn=()=>{};gn.inline=(e,{modifiers:t},{cleanup:r})=>{t.includes("self")?e._x_ignoreSelf=!0:e._x_ignore=!0,r(()=>{t.includes("self")?delete e._x_ignoreSelf:delete e._x_ignore})};d("ignore",gn);d("effect",A((e,{expression:t},{effect:r})=>{r(x(e,t))}));function se(e,t,r,n){let i=e,o=c=>n(c),s={},a=(c,l)=>u=>l(c,u);if(r.includes("dot")&&(t=zi(t)),r.includes("camel")&&(t=Ki(t)),r.includes("passive")&&(s.passive=!0),r.includes("capture")&&(s.capture=!0),r.includes("window")&&(i=window),r.includes("document")&&(i=document),r.includes("debounce")){let c=r[r.indexOf("debounce")+1]||"invalid-wait",l=et(c.split("ms")[0])?Number(c.split("ms")[0]):250;o=ze(o,l)}if(r.includes("throttle")){let c=r[r.indexOf("throttle")+1]||"invalid-wait",l=et(c.split("ms")[0])?Number(c.split("ms")[0]):250;o=Ke(o,l)}return r.includes("prevent")&&(o=a(o,(c,l)=>{l.preventDefault(),c(l)})),r.includes("stop")&&(o=a(o,(c,l)=>{l.stopPropagation(),c(l)})),r.includes("once")&&(o=a(o,(c,l)=>{c(l),i.removeEventListener(t,o,s)})),(r.includes("away")||r.includes("outside"))&&(i=document,o=a(o,(c,l)=>{e.contains(l.target)||l.target.isConnected!==!1&&(e.offsetWidth<1&&e.offsetHeight<1||e._x_isShown!==!1&&c(l))})),r.includes("self")&&(o=a(o,(c,l)=>{l.target===e&&c(l)})),(Vi(t)||yn(t))&&(o=a(o,(c,l)=>{qi(l,r)||c(l)})),i.addEventListener(t,o,s),()=>{i.removeEventListener(t,o,s)}}function zi(e){return e.replace(/-/g,".")}function Ki(e){return e.toLowerCase().replace(/-(\w)/g,(t,r)=>r.toUpperCase())}function et(e){return!Array.isArray(e)&&!isNaN(e)}function Hi(e){return[" ","_"].includes(e)?e:e.replace(/([a-z])([A-Z])/g,"$1-$2").replace(/[_\s]/,"-").toLowerCase()}function Vi(e){return["keydown","keyup"].includes(e)}function yn(e){return["contextmenu","click","mouse"].some(t=>e.includes(t))}function qi(e,t){let r=t.filter(o=>!["window","document","prevent","stop","once","capture","self","away","outside","passive"].includes(o));if(r.includes("debounce")){let o=r.indexOf("debounce");r.splice(o,et((r[o+1]||"invalid-wait").split("ms")[0])?2:1)}if(r.includes("throttle")){let o=r.indexOf("throttle");r.splice(o,et((r[o+1]||"invalid-wait").split("ms")[0])?2:1)}if(r.length===0||r.length===1&&xn(e.key).includes(r[0]))return!1;let i=["ctrl","shift","alt","meta","cmd","super"].filter(o=>r.includes(o));return r=r.filter(o=>!i.includes(o)),!(i.length>0&&i.filter(s=>((s==="cmd"||s==="super")&&(s="meta"),e[`${s}Key`])).length===i.length&&(yn(e.type)||xn(e.key).includes(r[0])))}function xn(e){if(!e)return[];e=Hi(e);let t={ctrl:"control",slash:"/",space:" ",spacebar:" ",cmd:"meta",esc:"escape",up:"arrow-up",down:"arrow-down",left:"arrow-left",right:"arrow-right",period:".",comma:",",equal:"=",minus:"-",underscore:"_"};return t[e]=e,Object.keys(t).map(r=>{if(t[r]===e)return r}).filter(r=>r)}d("model",(e,{modifiers:t,expression:r},{effect:n,cleanup:i})=>{let o=e;t.includes("parent")&&(o=e.parentNode);let s=x(o,r),a;typeof r=="string"?a=x(o,`${r} = __placeholder`):typeof r=="function"&&typeof r()=="string"?a=x(o,`${r()} = __placeholder`):a=()=>{};let c=()=>{let m;return s(w=>m=w),bn(m)?m.get():m},l=m=>{let w;s($=>w=$),bn(w)?w.set(m):a(()=>{},{scope:{__placeholder:m}})};typeof r=="string"&&e.type==="radio"&&_(()=>{e.hasAttribute("name")||e.setAttribute("name",r)});var u=e.tagName.toLowerCase()==="select"||["checkbox","radio"].includes(e.type)||t.includes("lazy")?"change":"input";let p=I?()=>{}:se(e,u,t,m=>{l(zt(e,t,m,c()))});if(t.includes("fill")&&([void 0,null,""].includes(c())||e.type==="checkbox"&&Array.isArray(c())||e.tagName.toLowerCase()==="select"&&e.multiple)&&l(zt(e,t,{target:e},c())),e._x_removeModelListeners||(e._x_removeModelListeners={}),e._x_removeModelListeners.default=p,i(()=>e._x_removeModelListeners.default()),e.form){let m=se(e.form,"reset",[],w=>{ne(()=>e._x_model&&e._x_model.set(zt(e,t,{target:e},c())))});i(()=>m())}e._x_model={get(){return c()},set(m){l(m)}},e._x_forceModelUpdate=m=>{m===void 0&&typeof r=="string"&&r.match(/\./)&&(m=""),window.fromModel=!0,_(()=>he(e,"value",m)),delete window.fromModel},n(()=>{let m=c();t.includes("unintrusive")&&document.activeElement.isSameNode(e)||e._x_forceModelUpdate(m)})});function zt(e,t,r,n){return _(()=>{if(r instanceof CustomEvent&&r.detail!==void 0)return r.detail!==null&&r.detail!==void 0?r.detail:r.target.value;if(e.type==="checkbox")if(Array.isArray(n)){let i=null;return t.includes("number")?i=Kt(r.target.value):t.includes("boolean")?i=ge(r.target.value):i=r.target.value,r.target.checked?n.includes(i)?n:n.concat([i]):n.filter(o=>!Ui(o,i))}else return r.target.checked;else{if(e.tagName.toLowerCase()==="select"&&e.multiple)return t.includes("number")?Array.from(r.target.selectedOptions).map(i=>{let o=i.value||i.text;return Kt(o)}):t.includes("boolean")?Array.from(r.target.selectedOptions).map(i=>{let o=i.value||i.text;return ge(o)}):Array.from(r.target.selectedOptions).map(i=>i.value||i.text);{let i;return e.type==="radio"?r.target.checked?i=r.target.value:i=n:i=r.target.value,t.includes("number")?Kt(i):t.includes("boolean")?ge(i):t.includes("trim")?i.trim():i}}})}function Kt(e){let t=e?parseFloat(e):null;return Wi(t)?t:e}function Ui(e,t){return e==t}function Wi(e){return!Array.isArray(e)&&!isNaN(e)}function bn(e){return e!==null&&typeof e=="object"&&typeof e.get=="function"&&typeof e.set=="function"}d("cloak",e=>queueMicrotask(()=>_(()=>e.removeAttribute(C("cloak")))));$e(()=>`[${C("init")}]`);d("init",A((e,{expression:t},{evaluate:r})=>typeof t=="string"?!!t.trim()&&r(t,{},!1):r(t,{},!1)));d("text",(e,{expression:t},{effect:r,evaluateLater:n})=>{let i=n(t);r(()=>{i(o=>{_(()=>{e.textContent=o})})})});d("html",(e,{expression:t},{effect:r,evaluateLater:n})=>{let i=n(t);r(()=>{i(o=>{_(()=>{e.innerHTML=o,e._x_ignoreSelf=!0,S(e),delete e._x_ignoreSelf})})})});re(Ie(":",ke(C("bind:"))));var wn=(e,{value:t,modifiers:r,expression:n,original:i},{effect:o,cleanup:s})=>{if(!t){let c={};Hr(c),x(e,n)(u=>{Ct(e,u,i)},{scope:c});return}if(t==="key")return Gi(e,n);if(e._x_inlineBindings&&e._x_inlineBindings[t]&&e._x_inlineBindings[t].extract)return;let a=x(e,n);o(()=>a(c=>{c===void 0&&typeof n=="string"&&n.match(/\./)&&(c=""),_(()=>he(e,t,c,r))})),s(()=>{e._x_undoAddedClasses&&e._x_undoAddedClasses(),e._x_undoAddedStyles&&e._x_undoAddedStyles()})};wn.inline=(e,{value:t,modifiers:r,expression:n})=>{t&&(e._x_inlineBindings||(e._x_inlineBindings={}),e._x_inlineBindings[t]={expression:n,extract:!1})};d("bind",wn);function Gi(e,t){e._x_keyExpression=t}Le(()=>`[${C("data")}]`);d("data",(e,{expression:t},{cleanup:r})=>{if(Ji(e))return;t=t===""?"{}":t;let n={};ue(n,e);let i={};Ur(i,n);let o=M(e,t,{scope:i});(o===void 0||o===!0)&&(o={}),ue(o,e);let s=R(o);Te(s);let a=P(e,s);s.init&&M(e,s.init),r(()=>{s.destroy&&M(e,s.destroy),a()})});K((e,t)=>{e._x_dataStack&&(t._x_dataStack=e._x_dataStack,t.setAttribute("data-has-alpine-state",!0))});function Ji(e){return I?Be?!0:e.hasAttribute("data-has-alpine-state"):!1}d("show",(e,{modifiers:t,expression:r},{effect:n})=>{let i=x(e,r);e._x_doHide||(e._x_doHide=()=>{_(()=>{e.style.setProperty("display","none",t.includes("important")?"important":void 0)})}),e._x_doShow||(e._x_doShow=()=>{_(()=>{e.style.length===1&&e.style.display==="none"?e.removeAttribute("style"):e.style.removeProperty("display")})});let o=()=>{e._x_doHide(),e._x_isShown=!1},s=()=>{e._x_doShow(),e._x_isShown=!0},a=()=>setTimeout(s),c=me(p=>p?s():o(),p=>{typeof e._x_toggleAndCascadeWithTransitions=="function"?e._x_toggleAndCascadeWithTransitions(e,p,s,o):p?a():o()}),l,u=!0;n(()=>i(p=>{!u&&p===l||(t.includes("immediate")&&(p?a():o()),c(p),l=p,u=!1)}))});d("for",(e,{expression:t},{effect:r,cleanup:n})=>{let i=Xi(t),o=x(e,i.items),s=x(e,e._x_keyExpression||"index");e._x_prevKeys=[],e._x_lookup={},r(()=>Yi(e,i,o,s)),n(()=>{Object.values(e._x_lookup).forEach(a=>a.remove()),delete e._x_prevKeys,delete e._x_lookup})});function Yi(e,t,r,n){let i=s=>typeof s=="object"&&!Array.isArray(s),o=e;r(s=>{Zi(s)&&s>=0&&(s=Array.from(Array(s).keys(),f=>f+1)),s===void 0&&(s=[]);let a=e._x_lookup,c=e._x_prevKeys,l=[],u=[];if(i(s))s=Object.entries(s).map(([f,g])=>{let b=En(t,g,f,s);n(v=>{u.includes(v)&&E("Duplicate key on x-for",e),u.push(v)},{scope:{index:f,...b}}),l.push(b)});else for(let f=0;f<s.length;f++){let g=En(t,s[f],f,s);n(b=>{u.includes(b)&&E("Duplicate key on x-for",e),u.push(b)},{scope:{index:f,...g}}),l.push(g)}let p=[],m=[],w=[],$=[];for(let f=0;f<c.length;f++){let g=c[f];u.indexOf(g)===-1&&w.push(g)}c=c.filter(f=>!w.includes(f));let we="template";for(let f=0;f<u.length;f++){let g=u[f],b=c.indexOf(g);if(b===-1)c.splice(f,0,g),p.push([we,f]);else if(b!==f){let v=c.splice(f,1)[0],O=c.splice(b-1,1)[0];c.splice(f,0,O),c.splice(b,0,v),m.push([v,O])}else $.push(g);we=g}for(let f=0;f<w.length;f++){let g=w[f];a[g]._x_effects&&a[g]._x_effects.forEach(Ee),a[g].remove(),a[g]=null,delete a[g]}for(let f=0;f<m.length;f++){let[g,b]=m[f],v=a[g],O=a[b],Q=document.createElement("div");_(()=>{O||E('x-for ":key" is undefined or invalid',o,b,a),O.after(Q),v.after(O),O._x_currentIfEl&&O.after(O._x_currentIfEl),Q.before(v),v._x_currentIfEl&&v.after(v._x_currentIfEl),Q.remove()}),O._x_refreshXForScope(l[u.indexOf(b)])}for(let f=0;f<p.length;f++){let[g,b]=p[f],v=g==="template"?o:a[g];v._x_currentIfEl&&(v=v._x_currentIfEl);let O=l[b],Q=u[b],ae=document.importNode(o.content,!0).firstElementChild,Vt=R(O);P(ae,Vt,o),ae._x_refreshXForScope=Sn=>{Object.entries(Sn).forEach(([An,On])=>{Vt[An]=On})},_(()=>{v.after(ae),A(()=>S(ae))()}),typeof Q=="object"&&E("x-for key cannot be an object, it must be a string or an integer",o),a[Q]=ae}for(let f=0;f<$.length;f++)a[$[f]]._x_refreshXForScope(l[u.indexOf($[f])]);o._x_prevKeys=u})}function Xi(e){let t=/,([^,\}\]]*)(?:,([^,\}\]]*))?$/,r=/^\s*\(|\)\s*$/g,n=/([\s\S]*?)\s+(?:in|of)\s+([\s\S]*)/,i=e.match(n);if(!i)return;let o={};o.items=i[2].trim();let s=i[1].replace(r,"").trim(),a=s.match(t);return a?(o.item=s.replace(t,"").trim(),o.index=a[1].trim(),a[2]&&(o.collection=a[2].trim())):o.item=s,o}function En(e,t,r,n){let i={};return/^\[.*\]$/.test(e.item)&&Array.isArray(t)?e.item.replace("[","").replace("]","").split(",").map(s=>s.trim()).forEach((s,a)=>{i[s]=t[a]}):/^\{.*\}$/.test(e.item)&&!Array.isArray(t)&&typeof t=="object"?e.item.replace("{","").replace("}","").split(",").map(s=>s.trim()).forEach(s=>{i[s]=t[s]}):i[e.item]=t,e.index&&(i[e.index]=r),e.collection&&(i[e.collection]=n),i}function Zi(e){return!Array.isArray(e)&&!isNaN(e)}function vn(){}vn.inline=(e,{expression:t},{cleanup:r})=>{let n=J(e);n._x_refs||(n._x_refs={}),n._x_refs[t]=e,r(()=>delete n._x_refs[t])};d("ref",vn);d("if",(e,{expression:t},{effect:r,cleanup:n})=>{e.tagName.toLowerCase()!=="template"&&E("x-if can only be used on a <template> tag",e);let i=x(e,t),o=()=>{if(e._x_currentIfEl)return e._x_currentIfEl;let a=e.content.cloneNode(!0).firstElementChild;return P(a,{},e),_(()=>{e.after(a),A(()=>S(a))()}),e._x_currentIfEl=a,e._x_undoIf=()=>{T(a,c=>{c._x_effects&&c._x_effects.forEach(Ee)}),a.remove(),delete e._x_currentIfEl},a},s=()=>{e._x_undoIf&&(e._x_undoIf(),delete e._x_undoIf)};r(()=>i(a=>{a?o():s()})),n(()=>e._x_undoIf&&e._x_undoIf())});d("id",(e,{expression:t},{evaluate:r})=>{r(t).forEach(i=>mn(e,i))});K((e,t)=>{e._x_ids&&(t._x_ids=e._x_ids)});re(Ie("@",ke(C("on:"))));d("on",A((e,{value:t,modifiers:r,expression:n},{cleanup:i})=>{let o=n?x(e,n):()=>{};e.tagName.toLowerCase()==="template"&&(e._x_forwardEvents||(e._x_forwardEvents=[]),e._x_forwardEvents.includes(t)||e._x_forwardEvents.push(t));let s=se(e,t,r,a=>{o(()=>{},{scope:{$event:a},params:[a]})});i(()=>s())}));tt("Collapse","collapse","collapse");tt("Intersect","intersect","intersect");tt("Focus","trap","focus");tt("Mask","mask","mask");function tt(e,t,r){d(t,n=>E(`You can't use [x-${t}] without first installing the "${e}" plugin here: https://alpinejs.dev/plugins/${r}`,n))}B.setEvaluator(gt);B.setReactivityEngine({reactive:Qe,effect:en,release:tn,raw:h});var Ht=B;window.Alpine=Ht;queueMicrotask(()=>{Ht.start()});})();


(function () {
  'use strict';

  // ---- Tab switching (UX9 grouped tabs) -----------------------------------
  // The top bar's .tab[data-tab=<group>] buttons swap the
  // .tab-group-pane[data-tab-group] panes; inside each group a pill row of
  // .subtab[data-subtab=<section>] buttons swaps the .subtab-pane[data-tab]
  // section panes. Section panes keep the legacy per-section data-tab ids so
  // saved comment anchors + cross-links keep resolving after the grouping.
  var topTabs = document.querySelectorAll('.tabs .tab[data-tab]');
  topTabs.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var id = btn.getAttribute('data-tab');
      topTabs.forEach(function (t) {
        t.classList.toggle('active', t === btn);
        if (t === btn) t.setAttribute('aria-current', 'page');
        else t.removeAttribute('aria-current');
      });
      document.querySelectorAll('.tab-group-pane[data-tab-group]').forEach(function (p) {
        p.classList.toggle('active', p.getAttribute('data-tab-group') === id);
      });
    });
  });

  document.querySelectorAll('.tab-group-pane').forEach(function (pane) {
    var pills = pane.querySelectorAll('.subtab[data-subtab]');
    pills.forEach(function (pill) {
      pill.addEventListener('click', function () {
        var id = pill.getAttribute('data-subtab');
        pills.forEach(function (p) {
          p.classList.toggle('active', p === pill);
        });
        pane.querySelectorAll('.subtab-pane[data-tab]').forEach(function (sp) {
          sp.classList.toggle('active', sp.getAttribute('data-tab') === id);
        });
      });
    });
  });

  // Activate a tab by section OR group id. Section ids ("earnings", "bear",
  // ...) resolve through their pill (activating the owning group first);
  // single-section groups reuse the section id as the group id, so the
  // top-bar fallback covers them. Returns true when something matched.
  function activateSection(id) {
    if (!id) return false;
    var pill = document.querySelector('.subtab[data-subtab="' + id + '"]');
    if (pill) {
      var pane = pill.closest('.tab-group-pane');
      var gid = pane ? pane.getAttribute('data-tab-group') : null;
      var groupBtn = gid
        ? document.querySelector('.tabs .tab[data-tab="' + gid + '"]')
        : null;
      if (groupBtn) groupBtn.click();
      pill.click();
      return true;
    }
    var topBtn = document.querySelector('.tabs .tab[data-tab="' + id + '"]');
    if (topBtn) { topBtn.click(); return true; }
    return false;
  }

  // (Q&A accordion: now native <details class="qa-row"> — no JS. P4.1.)

  // ---- Cross-tab links (P4.3) ---------------------------------------------
  // <a data-xtab="bear" data-anchor="panel-failure-modes"> switches to the
  // named section's tab and scrolls the anchor panel into view (or the top
  // when no anchor). Authored by workspace_html._xlink_html.
  document.querySelectorAll('a[data-xtab]').forEach(function (link) {
    link.addEventListener('click', function (ev) {
      ev.preventDefault();
      activateSection(link.getAttribute('data-xtab'));
      var anchorId = link.getAttribute('data-anchor');
      var target = anchorId ? document.getElementById(anchorId) : null;
      if (target) {
        target.scrollIntoView({behavior: 'smooth', block: 'start'});
        target.classList.add('xlink-flash');
        setTimeout(function () { target.classList.remove('xlink-flash'); }, 1600);
      } else {
        var root = document.querySelector('.l1-root');
        if (root) root.scrollTop = 0;
      }
    });
  });

  // ---- Deep links: #tab=<section-or-group id> ------------------------------
  // Old per-section links (#tab=earnings) land on the right group + pill.
  function applyHash() {
    var m = /^#tab=([\w-]+)$/.exec(location.hash || '');
    if (m) activateSection(m[1]);
  }
  window.addEventListener('hashchange', applyHash);
  applyHash();

  // ---- Quarter selector ---------------------------------------------------
  // Quarter labels ("Q1 2026") are shared across groups (earnings + saydo
  // sit side by side; ir lives in another tab) — a click on one group's
  // button broadcasts the same quarter to every sibling group that has a
  // button for it, so switching the quarter in one place keeps every quarter
  // card on the page in sync. A group with no matching button is a silent
  // no-op (it may not cover that quarter).
  var quarterGroups = document.querySelectorAll('[data-quarter-group]');
  function swapQuarterGroup(group, q) {
    var groupId = group.getAttribute('data-quarter-group');
    group.querySelectorAll('button[data-quarter]').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-quarter') === q);
    });
    document
      .querySelectorAll('[data-quarter-card][data-quarter-group="' + groupId + '"]')
      .forEach(function (card) {
        var match = card.getAttribute('data-quarter') === q;
        card.style.display = match ? '' : 'none';
      });
  }
  quarterGroups.forEach(function (group) {
    group.querySelectorAll('button[data-quarter]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var q = btn.getAttribute('data-quarter');
        swapQuarterGroup(group, q);
        quarterGroups.forEach(function (other) {
          if (other === group) return;
          var sibling = other.querySelector('button[data-quarter="' + q + '"]');
          if (sibling) swapQuarterGroup(other, q);
        });
      });
    });
  });

  // ---- Financials line-item drill-down -----------------------------------
  // Click a .fin-row.drillable to toggle the .fin-drill row whose id matches
  // data-drill-target. Updates the ▶ chevron to ▼ when open.
  document.querySelectorAll('.fin-row.drillable').forEach(function (row) {
    row.addEventListener('click', function () {
      var targetId = row.getAttribute('data-drill-target');
      if (!targetId) return;
      var target = document.getElementById(targetId);
      if (!target) return;
      var isOpen = target.style.display !== 'none';
      target.style.display = isOpen ? 'none' : '';
      var chev = row.querySelector('.fin-chev');
      if (chev) chev.textContent = isOpen ? '▶' : '▼';
    });
  });

  // ---- Collapsible persist: <details data-persist="key"> remembers state ---
  // Open/closed state survives reopen via localStorage (works offline). Silent
  // no-op when storage is unavailable (some file:// sandboxes).
  document.querySelectorAll('details[data-persist]').forEach(function (d) {
    var key = 'ws:det:' + d.getAttribute('data-persist');
    try {
      var saved = localStorage.getItem(key);
      if (saved === 'open') d.open = true;
      else if (saved === 'closed') d.open = false;
    } catch (e) {}
    d.addEventListener('toggle', function () {
      try { localStorage.setItem(key, d.open ? 'open' : 'closed'); } catch (e) {}
    });
  });

  // ---- Keyboard shortcuts: j/k panels · / filter · ? help · Esc -----------
  function inField(el) {
    return (
      !!el &&
      (el.tagName === 'INPUT' ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'SELECT' ||
        el.isContentEditable)
    );
  }
  function visiblePanels() {
    return Array.prototype.filter.call(document.querySelectorAll('.panel'), function (p) {
      return p.offsetParent !== null;
    });
  }
  function movePanel(dir) {
    var panels = visiblePanels();
    if (!panels.length) return;
    var idx = -1;
    for (var i = 0; i < panels.length; i++) {
      if (panels[i].getBoundingClientRect().top <= 10) idx = i;
    }
    var next = Math.max(0, Math.min(panels.length - 1, idx + dir));
    panels[next].scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function focusFilter() {
    var filters = Array.prototype.filter.call(
      document.querySelectorAll('.lg-filter'),
      function (f) {
        return f.offsetParent !== null;
      }
    );
    if (!filters.length) return false;
    var pick = filters[0];
    for (var i = 0; i < filters.length; i++) {
      if (filters[i].getBoundingClientRect().top > 0) {
        pick = filters[i];
        break;
      }
    }
    pick.focus();
    pick.select();
    return true;
  }
  var helpEl = null;
  function buildHelp() {
    if (helpEl) return helpEl;
    helpEl = document.createElement('div');
    helpEl.className = 'ws-kbd-help';
    helpEl.setAttribute('role', 'dialog');
    helpEl.setAttribute('aria-label', 'Keyboard shortcuts');
    helpEl.innerHTML =
      '<div class="ws-kbd-card"><div class="ws-kbd-title">Keyboard shortcuts</div>' +
      '<dl class="ws-kbd-list">' +
      '<dt>j / k</dt><dd>next / previous panel</dd>' +
      '<dt>/</dt><dd>focus the table filter</dd>' +
      '<dt>?</dt><dd>toggle this help</dd>' +
      '</dl><div class="ws-kbd-hint">click or ? to close</div></div>';
    helpEl.addEventListener('click', function () {
      hideHelp();
    });
    document.body.appendChild(helpEl);
    return helpEl;
  }
  function showHelp() {
    buildHelp().classList.add('is-open');
  }
  function hideHelp() {
    if (helpEl) helpEl.classList.remove('is-open');
  }
  function helpOpen() {
    return !!helpEl && helpEl.classList.contains('is-open');
  }
  // NB: Escape is intentionally NOT handled here — CCOverlay owns the single
  // Escape handler for this document (one-Escape design law). The help overlay
  // closes on click or a second ? press.
  document.addEventListener('keydown', function (ev) {
    if (ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (inField(document.activeElement)) return;
    if (ev.key === 'j') {
      ev.preventDefault();
      movePanel(1);
    } else if (ev.key === 'k') {
      ev.preventDefault();
      movePanel(-1);
    } else if (ev.key === '/') {
      if (focusFilter()) ev.preventDefault();
    } else if (ev.key === '?') {
      ev.preventDefault();
      if (helpOpen()) hideHelp();
      else showHelp();
    }
  });

  // ---- Initial highlight: ensure the first top tab is active if none set --
  var anyActive = document.querySelector('.tabs .tab.active');
  if (!anyActive && topTabs.length) topTabs[0].click();
})();


(function () {
  if (window.CCOverlay) return;

  // ---- in-memory open-surface stack (ephemeral; NOT cc_state sessionStorage) ----
  var surfaces = [];     // every registered MODAL surface
  var dismissers = [];   // non-modal Escape-only closers: fn() -> bool (closed?)
  var seqCounter = 0;    // recency tie-break for equal priorities

  // ---- ONE shared scrim (S1's .k-scrim look; CCOverlay owns z-index + click) ----
  var scrim = document.createElement('div');
  scrim.className = 'k-scrim';
  scrim.id = 'cc-overlay-scrim';
  scrim.hidden = true;
  scrim.setAttribute('aria-hidden', 'true');
  function ensureScrim() {
    if (!scrim.parentNode && document.body) document.body.appendChild(scrim);
  }
  scrim.addEventListener('click', function () {
    var s = topScrimSurface();
    if (s) doClose(s);
  });

  function reduceMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function zOf(el) {
    var z = el ? parseInt(getComputedStyle(el).zIndex, 10) : 0;
    return isNaN(z) ? 0 : z;
  }

  function focusableIn(c) {
    if (!c) return [];
    return Array.prototype.slice.call(c.querySelectorAll(
      'button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),' +
      'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
    )).filter(function (el) { return el.offsetParent !== null || el === document.activeElement; });
  }

  // The top open MODAL surface BY PRIORITY (palette > peek > drawer > dock) —
  // never merely the most-recently-opened. Ties (same priority) fall back to
  // recency so two equal surfaces still resolve deterministically.
  function topModalSurface() {
    var best = null;
    for (var i = 0; i < surfaces.length; i++) {
      var s = surfaces[i];
      if (!s.isOpen || !s.opts.modal) continue;
      if (!best ||
          s.opts.priority > best.opts.priority ||
          (s.opts.priority === best.opts.priority && s.seq > best.seq)) {
        best = s;
      }
    }
    return best;
  }

  // The scrim sits beneath the VISUALLY topmost scrim-requesting surface, so
  // resolve by computed z-index (a surface may opt out of the scrim while a
  // lower one keeps it).
  function topScrimSurface() {
    var best = null, bestZ = -1;
    for (var i = 0; i < surfaces.length; i++) {
      var s = surfaces[i];
      if (!s.isOpen || !s.opts.modal || !s.opts.scrim) continue;
      var z = zOf(s.el);
      if (!best || z >= bestZ) { best = s; bestZ = z; }
    }
    return best;
  }

  // Snap the scrim under the top scrim-requesting surface, or hide it. The
  // FADE-out on the last surface leaving is driven concurrently from doClose
  // (so scrim + surface animate together), not here.
  function syncScrim() {
    var s = topScrimSurface();
    if (s) {
      ensureScrim();
      scrim.classList.remove('cc-scrim-out');
      scrim.style.zIndex = String(zOf(s.el) - 1);
      // Default scrim alpha rides the .k-scrim class (var(--scrim)); a custom
      // scrimOpacity composes a neutral black veil at that alpha without a raw
      // rgba literal (token discipline — see tests/test_ui_controls.py color dim).
      scrim.style.background = (s.opts.scrimOpacity != null)
        ? 'color-mix(in srgb, black ' + (s.opts.scrimOpacity * 100) + '%, transparent)' : '';
      scrim.hidden = false;
    } else {
      scrim.classList.remove('cc-scrim-out');
      scrim.hidden = true;
    }
  }

  // The symmetric close: animate the surface out along its open axis, then
  // hide. Falls straight through when motion is off / disabled.
  function animateOut(el, motion, done) {
    if (!el || motion === 'none' || reduceMotion()) { done(); return; }
    var mcls = 'cc-m-' + motion;
    el.classList.add('cc-anim-out', mcls);
    var finished = false;
    function fin() {
      if (finished) return; finished = true;
      el.removeEventListener('transitionend', onEnd);
      el.classList.remove('cc-anim-out', mcls);
      done();
    }
    function onEnd(e) { if (e.target === el) fin(); }
    el.addEventListener('transitionend', onEnd);
    setTimeout(fin, 240);  // fallback if transitionend never fires
  }

  function doOpen(s) {
    if (s.isOpen) return;
    // Mutual exclusion: opening a grouped surface closes its open siblings.
    if (s.opts.group) {
      for (var i = 0; i < surfaces.length; i++) {
        var o = surfaces[i];
        if (o !== s && o.isOpen && o.opts.group === s.opts.group) doClose(o);
      }
    }
    if (s.opts.restoreFocus) s.opener = document.activeElement;
    s.isOpen = true;
    s.seq = ++seqCounter;
    if (s.el) {
      s.el.classList.remove('cc-anim-out', 'cc-m-rise', 'cc-m-slide-right', 'cc-m-pop');
      // A persistent surface (e.g. the dock) drives its own visibility via a
      // data-attr/CSS — CCOverlay only tracks it for Escape/scrim — so it opts
      // out of the [hidden] toggle.
      if (s.opts.toggleHidden !== false) s.el.hidden = false;
    }
    syncScrim();
    if (s.opts.onOpen) { try { s.opts.onOpen(); } catch (e) {} }
    // Focus: closeId by default; a surface that drives its own focus passes
    // autofocus:false (and focuses in onOpen); autofocus:'<id>' overrides.
    if (s.opts.autofocus !== false) {
      var target = null;
      if (typeof s.opts.autofocus === 'string') target = document.getElementById(s.opts.autofocus);
      if (!target && s.opts.closeId) target = document.getElementById(s.opts.closeId);
      if (!target && s.el) target = focusableIn(s.el)[0] || null;
      if (target && target.focus) { try { target.focus(); } catch (e) {} }
    }
  }

  function doClose(s) {
    if (!s.isOpen) return;  // idempotent — re-entrant onClose calls are no-ops
    s.isOpen = false;
    var el = s.el;
    // With s.isOpen now false, this reflects who still needs the scrim AFTER s
    // leaves. If nobody does, fade the scrim out concurrently with the surface.
    var stillScrim = topScrimSurface();
    if (s.opts.scrim && !stillScrim && !scrim.hidden && !reduceMotion()) {
      scrim.classList.add('cc-scrim-out');
    }
    function finish() {
      if (el && s.opts.toggleHidden !== false) el.hidden = true;
      if (stillScrim) syncScrim();  // reposition under the now-top surface
      else { scrim.classList.remove('cc-scrim-out'); scrim.hidden = true; }
      if (s.opts.onClose) { try { s.opts.onClose(); } catch (e) {} }
      if (s.opts.restoreFocus && s.opener && s.opener.focus) {
        try { s.opener.focus(); } catch (e) {}
        s.opener = null;
      }
    }
    animateOut(el, s.opts.motion, finish);
  }

  // ---- ONE keydown listener: Escape (priority-resolved) + Tab (focus trap) --
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') {
      // Non-modal popovers (cite-marks / source-chip / hover) claim Escape
      // first — they are the innermost, lightest layer.
      for (var i = dismissers.length - 1; i >= 0; i--) {
        var closed = false;
        try { closed = dismissers[i](); } catch (e) {}
        if (closed) { ev.preventDefault(); return; }
      }
      var top = topModalSurface();
      if (top) { ev.preventDefault(); doClose(top); }
      return;
    }
    if (ev.key === 'Tab') {
      var m = topModalSurface();
      if (!m || !m.opts.trapFocus || !m.el) return;
      var els = focusableIn(m.el);
      if (!els.length) return;
      var first = els[0], last = els[els.length - 1];
      if (ev.shiftKey) {
        if (document.activeElement === first || !m.el.contains(document.activeElement)) {
          ev.preventDefault(); last.focus();
        }
      } else if (document.activeElement === last) {
        ev.preventDefault(); first.focus();
      }
    }
  });

  function register(el, opts) {
    opts = opts || {};
    if (opts.modal === undefined) opts.modal = true;
    opts.priority = opts.priority || 0;
    opts.scrim = !!opts.scrim;
    opts.trapFocus = !!opts.trapFocus;
    opts.restoreFocus = opts.restoreFocus !== false;  // default: restore
    opts.motion = opts.motion || 'rise';
    var s = { el: el, opts: opts, isOpen: false, seq: 0, opener: null };
    surfaces.push(s);
    // The close control (x): auto-wire its click to dismiss. A surface whose
    // close control is a multi-state toggle (e.g. the dock's collapse-one-level
    // x) declares closeId for the contract + default focus but passes
    // wireClose:false to drive close from its own listener instead.
    if (opts.closeId && opts.wireClose !== false) {
      var btn = document.getElementById(opts.closeId);
      if (btn) {
        btn.addEventListener('click', function (e) {
          if (!s.isOpen) return;
          if (e && e.preventDefault) e.preventDefault();
          doClose(s);
        });
      }
    }
    return {
      open: function () { doOpen(s); },
      close: function () { doClose(s); },
      isOpen: function () { return s.isOpen; },
      el: el,
    };
  }

  window.CCOverlay = {
    register: register,
    // Non-modal Escape-only dismissal for phrasing-content popovers. fn() must
    // close at most ONE open popover and return whether it did.
    addPopoverDismisser: function (fn) { if (typeof fn === 'function') dismissers.push(fn); },
    // Priority ladder (matches the z-order): higher wins Escape.
    PRIORITY: { DOCK: 10, DRAWER: 30, PEEK: 40, PALETTE: 50 },
  };
})();


(function () {
  if (window.CCAction) return;

  function reduceMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  // Pressed state: disable + aria-busy (the kit dims [aria-busy]) + optional
  // busy label. The original label is stashed once in data-cc-label so
  // busy → release round-trips even if called twice. The stash happens ONLY
  // when a label swap is requested: the control may be a <select> (triage's
  // route picker), where writing textContent back would destroy the options.
  function busy(btn, label) {
    if (!btn) return;
    if (label) {
      if (!btn.hasAttribute('data-cc-label')) {
        btn.setAttribute('data-cc-label', btn.textContent);
      }
      btn.textContent = label;
    }
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
  }

  function release(btn) {
    if (!btn) return;
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    var orig = btn.getAttribute('data-cc-label');
    if (orig !== null) {
      btn.textContent = orig;
      btn.removeAttribute('data-cc-label');
    }
  }

  // Terminal receipt: the button stays where it was, disabled, showing what
  // just happened ("✓ Dismissed") — for surfaces whose element stays in place.
  // Buttons only (writes textContent), never a <select>.
  function receipt(btn, text) {
    if (!btn) return;
    btn.disabled = true;
    btn.removeAttribute('aria-busy');  // settled, not in flight
    btn.textContent = text;
  }

  // Animated removal: fade+settle, then collapse the pinned height so
  // siblings slide up, then remove from the DOM and call done().
  // transitionend drives each beat with a timeout fallback (240ms > the
  // 150ms token) mirroring CCOverlay.animateOut.
  function leave(el, done) {
    if (!el || !el.parentNode) { if (done) done(); return; }
    function finish() {
      if (el.parentNode) el.parentNode.removeChild(el);
      if (done) done();
    }
    if (reduceMotion()) { finish(); return; }
    var phase = 0;
    var timer = null;
    function next() {
      if (phase === 0) {
        phase = 1;
        el.classList.add('cc-act-collapse');
        timer = setTimeout(next, 240);
      } else if (phase === 1) {
        phase = 2;
        finish();
      }
    }
    el.addEventListener('transitionend', function (e) {
      if (e.target !== el) return;
      if ((phase === 0 && e.propertyName === 'opacity') ||
          (phase === 1 && e.propertyName === 'height')) {
        clearTimeout(timer);
        next();
      }
    });
    // Pin the box height BEFORE animating so beat 2 has a concrete start.
    el.style.height = el.offsetHeight + 'px';
    el.style.overflow = 'hidden';
    void el.offsetHeight;  // commit the pin before the class flips
    el.classList.add('cc-act-leave');
    timer = setTimeout(next, 240);
  }

  window.CCAction = {
    busy: busy,
    release: release,
    receipt: receipt,
    leave: leave,
  };
})();


(function () {
  if (window.__ccSrcChipEsc || !window.CCOverlay) return;
  window.__ccSrcChipEsc = true;
  window.CCOverlay.addPopoverDismisser(function () {
    var open = document.querySelectorAll('details.src-pop[open]');
    if (open.length) { open[open.length - 1].removeAttribute('open'); return true; }
    return false;
  });
})();


(function() {
  // ---------------------------------------------------------------
  // Boot data — embedded by the renderer.
  // ---------------------------------------------------------------
  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  var boot = readJson('workspace-boot');
  var commentStore = readJson('workspace-comments') || {comments: []};
  if (!boot) return;  // No boot data — comments feature disabled.

  var SERVER_URL = /^https?:$/.test(window.location.protocol)
    ? window.location.origin
    : (boot.server_url || 'http://localhost:7421');
  var TICKER = boot.ticker;
  var REPORT_DATE = boot.report_date;
  var MUTATION_HEADERS = {'Content-Type': 'application/json'};
  if (boot.report_capability) {
    MUTATION_HEADERS['X-Report-Capability'] = boot.report_capability;
  }
  window.__workspaceMutationHeaders = MUTATION_HEADERS;

  // Allow the chat module to share boot + comment refs.
  window.__workspaceCommentBoot = boot;
  window.__workspaceCommentStore = commentStore;

  // Navigation links must follow the server that delivered an HTTP report
  // (including a Tailscale address), while standalone file:// reports retain
  // the configured localhost fallback.
  document.querySelectorAll('a[data-server-path]').forEach(function(link) {
    var path = link.getAttribute('data-server-path');
    if (path && path.charAt(0) === '/') link.href = SERVER_URL + path;
  });

  // ---------------------------------------------------------------
  // Draft autosave — survive tab close / refresh / server-down with
  // unposted text. Drafts are keyed by (ticker, report_date, anchor)
  // and cleared on a successful POST. localStorage only — no server
  // round-trip. See test_workspace_comments_drafts.py.
  // ---------------------------------------------------------------
  function draftKey(anchor) {
    if (!anchor) return null;
    return 'cmt-draft:' + TICKER + ':' + REPORT_DATE
         + ':' + anchor.type + ':' + (anchor.key || '');
  }
  function saveDraft(anchor, text) {
    var k = draftKey(anchor);
    if (!k) return;
    try {
      if (text && text.length) localStorage.setItem(k, text);
      else localStorage.removeItem(k);
    } catch (e) { /* quota / disabled — silent */ }
  }
  function loadDraft(anchor) {
    var k = draftKey(anchor);
    if (!k) return '';
    try { return localStorage.getItem(k) || ''; }
    catch (e) { return ''; }
  }
  function clearDraft(anchor) {
    var k = draftKey(anchor);
    if (!k) return;
    try { localStorage.removeItem(k); } catch (e) { /* silent */ }
  }

  // ---------------------------------------------------------------
  // Outbox — when a POST fails (server down, network blip), the
  // payload is queued in localStorage and retried on a timer / focus
  // / online event until it lands. Distinct from draft autosave:
  // drafts are unposted text the user is still composing; outbox
  // entries are *posts the user already committed to* that we owe
  // them durability for. See test_workspace_comments_outbox.py.
  // ---------------------------------------------------------------
  var OUTBOX_KEY = 'cmt-outbox';
  var OUTBOX_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;  // drop entries older than 7d
  var OUTBOX_FLUSH_INTERVAL_MS = 15000;
  var outboxFlushing = false;

  function loadOutbox() {
    try { return JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]') || []; }
    catch (e) { return []; }
  }
  function saveOutbox(items) {
    try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(items)); }
    catch (e) { /* quota / disabled — silent */ }
  }
  function enqueueOutbox(payload) {
    var items = loadOutbox();
    items.push({
      id: 'q_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
      payload: payload,
      ts: Date.now()
    });
    saveOutbox(items);
    updateOutboxBadge();
  }
  function updateOutboxBadge() {
    var badge = document.getElementById('cmt-outbox-badge');
    if (!badge) return;
    var n = loadOutbox().length;
    badge.textContent = n ? ('Queued: ' + n) : '';
    badge.style.display = n ? 'inline-block' : 'none';
  }

  // Sequentially POST queued entries. Stops on the first failure so
  // ordering is preserved and we don't hammer a still-down server.
  // Re-entrancy guard (outboxFlushing) keeps the timer + focus event
  // from doubling up on the same in-flight flush.
  function flushOutbox() {
    if (outboxFlushing) return Promise.resolve();
    outboxFlushing = true;
    return (async function() {
      try {
        var items = loadOutbox();
        var now = Date.now();
        var fresh = items.filter(function(it) {
          return (now - (it.ts || 0)) < OUTBOX_MAX_AGE_MS;
        });
        if (fresh.length !== items.length) {
          console.warn('cmt-outbox: dropped ' + (items.length - fresh.length) + ' expired entries');
          saveOutbox(fresh);
        }
        for (var i = 0; i < fresh.length; i++) {
          var it = fresh[i];
          var r;
          try {
            r = await fetch(SERVER_URL + '/comments', {
              method: 'POST',
              headers: MUTATION_HEADERS,
              body: JSON.stringify(it.payload)
            });
          } catch (err) {
            break;  // still offline — leave the remainder for the next tick
          }
          if (!r.ok) break;  // server-side error — don't drop, retry later
          var created = await r.json();
          commentStore.comments.push(created);
          var remaining = loadOutbox().filter(function(x) { return x.id !== it.id; });
          saveOutbox(remaining);
          clearDraft(it.payload.anchor);
        }
        updateOutboxBadge();
        renderList();
        renderPins();
      } finally {
        outboxFlushing = false;
      }
    })();
  }

  // Wake-up triggers. setInterval keeps a steady cadence; focus + online
  // catch the user-driven recovery moments. window.__flushOutbox is exposed
  // for the health-pill (just below) to call on detected server recovery
  // without needing to know our internals.
  setInterval(flushOutbox, OUTBOX_FLUSH_INTERVAL_MS);
  window.addEventListener('online', flushOutbox);
  window.addEventListener('focus', flushOutbox);
  window.__flushOutbox = flushOutbox;

  // ---------------------------------------------------------------
  // Health pill — periodic GET /healthz tells the user up-front
  // whether the server is reachable, instead of finding out only
  // when they click Post. Drives the green/red pill in the sidebar
  // header and an inline "offline" banner above the textarea.
  //
  // On the offline → online edge we kick a flushOutbox() rather than
  // waiting for the next 15s tick — the user sees the queue drain
  // moments after the server is back. See test_workspace_comments_health.py.
  // ---------------------------------------------------------------
  var HEALTH_POLL_MS = 10000;
  var healthState = 'unknown';  // 'online' | 'offline' | 'unknown'

  function setHealthState(next) {
    var prev = healthState;
    if (next === prev) return;
    healthState = next;
    renderHealthPill();
    renderOfflineBanner();
    // Edge: offline → online → drain the queue immediately.
    if (prev === 'offline' && next === 'online' && typeof window.__flushOutbox === 'function') {
      window.__flushOutbox();
    }
  }

  function renderHealthPill() {
    var pill = document.getElementById('cmt-health-pill');
    if (!pill) return;
    // Filled status pill = the control kit's .k-pill (+ tone by meaning);
    // unknown -> neutral bare. cmt-health-* kept as the state hook.
    var tone = healthState === 'online' ? ' k-pill-ok'
             : healthState === 'offline' ? ' k-pill-bad' : '';
    pill.className = 'k-pill' + tone + ' cmt-health-pill cmt-health-' + healthState;
    pill.title = healthState === 'online'
      ? 'Server reachable.'
      : healthState === 'offline'
        ? 'Server unreachable — new comments will queue locally and sync on recovery.'
        : 'Checking server status…';
    pill.textContent = healthState === 'online' ? '● Online'
                      : healthState === 'offline' ? '● Offline'
                      : '○ …';
  }

  function renderOfflineBanner() {
    var banner = document.getElementById('cmt-offline-banner');
    if (!banner) return;
    banner.style.display = healthState === 'offline' ? 'block' : 'none';
  }

  function pollHealth() {
    // cache:no-store so a stale 200 doesn't mask a server that died
    // between the last poll and now. Manual AbortController timeout
    // keeps an unresponsive socket from blocking the next tick.
    // Returns the underlying promise so callers (Fix 3 tests, manual
    // debug) can await state-settling, instead of fire-and-forget.
    var ctrl = new AbortController();
    var killer = setTimeout(function() { ctrl.abort(); }, 5000);
    return fetch(SERVER_URL + '/healthz', {cache: 'no-store', signal: ctrl.signal})
      .then(function(r) {
        clearTimeout(killer);
        setHealthState(r.ok ? 'online' : 'offline');
      })
      .catch(function() {
        clearTimeout(killer);
        setHealthState('offline');
      });
  }
  setInterval(pollHealth, HEALTH_POLL_MS);
  window.addEventListener('focus', pollHealth);
  window.__pollCommentHealth = pollHealth;  // for Fix 3 tests + manual debug

  // ---------------------------------------------------------------
  // Pin rendering — annotate each [data-commentable] element with a
  // pin button + count of open comments.
  // ---------------------------------------------------------------
  function commentsForAnchor(type, key) {
    return commentStore.comments.filter(function(c) {
      return c.anchor && c.anchor.type === type && c.anchor.key === key;
    });
  }

  function renderPins() {
    var nodes = document.querySelectorAll('[data-commentable="true"]');
    nodes.forEach(function(node) {
      // Avoid double-pinning
      if (node.querySelector(':scope > .cmt-pin-host')) return;
      var type = node.getAttribute('data-anchor-type');
      var key = node.getAttribute('data-anchor-key');
      if (!type || !key) return;
      var pin = document.createElement('div');
      pin.className = 'cmt-pin-host';
      pin.innerHTML = pinMarkup(commentsForAnchor(type, key));
      node.appendChild(pin);
      pin.addEventListener('click', function(ev) {
        ev.stopPropagation();
        openSidebar(type, key, node);
      });
    });
  }

  function pinMarkup(commentList) {
    var openCount = commentList.filter(function(c) { return c.status === 'open'; }).length;
    var totalCount = commentList.length;
    var cls = 'cmt-pin';
    if (openCount > 0) cls += ' has-open';
    else if (totalCount > 0) cls += ' all-addressed';
    var label = totalCount ? totalCount : '+';
    var title = totalCount
      ? (openCount + ' open · ' + (totalCount - openCount) + ' addressed')
      : 'Comment';
    return '<button class="' + cls + '" title="' + title + '" type="button">' + label + '</button>';
  }

  // ---------------------------------------------------------------
  // Sidebar — static shell rendered by the Python template. Opens
  // when a pin / mark / floater-button is activated; lists comments
  // for that anchor + a "new comment" form. Dismiss via the close button
  // or Escape — no outside-click listener (it raced with mousedown-
  // triggered opens from the selection floater and closed the
  // sidebar on the same gesture that opened it).
  // ---------------------------------------------------------------
  var sidebar = document.getElementById('cmt-sidebar');
  var currentAnchor = null;
  if (sidebar) {
    sidebar.querySelector('.cmt-close').addEventListener('click', closeSidebar);
    sidebar.querySelector('#cmt-form').addEventListener('submit', onSubmit);
    var saveNoteBtn = sidebar.querySelector('#cmt-save-note');
    if (saveNoteBtn) saveNoteBtn.addEventListener('click', onSaveNote);
    // Autosave the draft on every keystroke so a tab close / refresh /
    // server-down outage doesn't lose typed-but-unposted text.
    var draftArea = sidebar.querySelector('#cmt-form [name="comment"]');
    if (draftArea) {
      draftArea.addEventListener('input', function() {
        saveDraft(currentAnchor, draftArea.value);
      });
    }
    // Inject the outbox-status badge into the sidebar header. Server-
    // rendered shell stays minimal; the badge is dynamic anyway. Hidden
    // when empty so it doesn't clutter the header in the happy path.
    var head = sidebar.querySelector('.cmt-sidebar-head');
    if (head && !document.getElementById('cmt-outbox-badge')) {
      var badge = document.createElement('span');
      badge.id = 'cmt-outbox-badge';
      badge.className = 'k-pill k-pill-warn cmt-outbox-badge';
      badge.style.display = 'none';
      badge.title = 'Comments queued locally — will retry until the server is back.';
      head.appendChild(badge);
      updateOutboxBadge();
    }
    // Health pill (Fix 3) — same header, sits left of the close button
    // so the user sees server status at a glance whenever the sidebar
    // is open.
    if (head && !document.getElementById('cmt-health-pill')) {
      var pill = document.createElement('span');
      pill.id = 'cmt-health-pill';
      pill.className = 'k-pill cmt-health-pill cmt-health-unknown';
      pill.textContent = '○ …';
      // Insert before the close button so it sits at the right edge
      // of the header content, not after the close glyph.
      var closeBtn = head.querySelector('.cmt-close');
      if (closeBtn) head.insertBefore(pill, closeBtn);
      else head.appendChild(pill);
    }
    // Offline banner — above the textarea, shown only when health is
    // 'offline'. Tells the user their next submit will queue (not lose).
    var formEl = sidebar.querySelector('#cmt-form');
    if (formEl && !document.getElementById('cmt-offline-banner')) {
      var banner = document.createElement('div');
      banner.id = 'cmt-offline-banner';
      banner.className = 'cmt-offline-banner';
      banner.style.display = 'none';
      banner.textContent = 'Server offline — your comment will queue locally and sync on recovery.';
      formEl.insertBefore(banner, formEl.firstChild);
    }
    // Fire the first poll right away so the user doesn't wait 10s for
    // the initial state to populate.
    if (typeof pollHealth === 'function') pollHealth();
  }

  // Visual open/close — the push-sidebar's own .open class + width transition.
  function applyOpenVisual(open) {
    if (!sidebar) return;
    sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
    sidebar.classList.toggle('open', open);
    if (open) document.documentElement.style.setProperty('--sidebar-open-width', '380px');
    else document.documentElement.style.removeProperty('--sidebar-open-width');
  }

  // CCOverlay registration (S4, Law 3): a gesture push-sidebar. scrim:false is
  // a DELIBERATE, DECLARED carve-out — the no-outside-click dismissal is
  // load-bearing (an outside-click listener raced the floater's mousedown-open
  // and closed the sidebar on the same gesture; see the comment above the
  // sidebar wiring). motion:'none' + toggleHidden:false (its own .open class +
  // width transition drive visuals); grouped 'report-sidebar' so opening it
  // closes the chat sidebar (replaces the window.__close* handshake). Escape is
  // CCOverlay's one listener — no per-sidebar keydown.
  var cmtOv = window.CCOverlay && window.CCOverlay.register(sidebar, {
    modal: true, priority: 30, scrim: false, trapFocus: false, restoreFocus: true,
    motion: 'none', toggleHidden: false, autofocus: false,
    group: 'report-sidebar', closeId: 'cmt-close', wireClose: false,
    onOpen: function() { applyOpenVisual(true); },
    onClose: function() { applyOpenVisual(false); currentAnchor = null; }
  });

  // Single entry point — pins call with a humanAnchor label, floater /
  // marks supply their own free-text label.
  function openWithAnchor(anchor, label) {
    if (!sidebar) return;
    currentAnchor = anchor;
    // open() handles the visual open + one-open-at-a-time (closes chat via the
    // 'report-sidebar' group); idempotent when already open (content still
    // refreshes below for the new anchor).
    if (cmtOv) cmtOv.open(); else applyOpenVisual(true);
    document.getElementById('cmt-anchor-label').textContent = label;
    renderList();
    // Rehydrate the draft for this anchor (if any). Hint that a draft
    // is restored so the user knows where the text came from.
    var area = sidebar.querySelector('#cmt-form [name="comment"]');
    if (area) {
      var draft = loadDraft(anchor);
      area.value = draft;
      hint(draft ? 'Draft restored.' : '');
    }
  }

  function openSidebar(type, key, anchorNode) {
    // Capture the stable doorway handle (S12) when the anchored cell carries
    // one, so the comment — and the note it mirrors — re-binds across a metric
    // rename even though `key` (the display name) is what moved.
    var anchor = {
      type: type, key: key,
      tab: anchorNode.getAttribute('data-anchor-tab'),
      fact_ref: anchorNode.getAttribute('data-fact-ref') || null
    };
    openWithAnchor(anchor, humanAnchor(anchor));
  }

  function closeSidebar() {
    if (cmtOv) cmtOv.close(); else { applyOpenVisual(false); currentAnchor = null; }
  }

  function humanAnchor(a) {
    return (a.type.replace(/_/g, ' ') + ' · ' + a.key).substring(0, 80);
  }

  function renderList() {
    var list = document.getElementById('cmt-list');
    if (!list || !currentAnchor) return;
    var items = commentsForAnchor(currentAnchor.type, currentAnchor.key);
    if (items.length === 0) {
      list.innerHTML = '<div class="cmt-empty">No comments yet on this element.</div>';
      return;
    }
    list.innerHTML = items.map(renderCommentCard).join('');
    // Wire dismiss / mark-addressed buttons (only when server is reachable).
    list.querySelectorAll('[data-cmt-action]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var id = btn.getAttribute('data-cmt-id');
        var action = btn.getAttribute('data-cmt-action');
        updateComment(id, action, btn);
      });
    });
  }

  function renderCommentCard(c) {
    var statusClass = 'cmt-status-' + escapeHtml(c.status);
    var head = '<div class="cmt-card-head">'
      + '<span class="cmt-status ' + statusClass + '">' + escapeHtml(c.status) + '</span>'
      + (c.intent ? '<span class="cmt-intent">' + escapeHtml(c.intent) + '</span>' : '')
      + '<span class="cmt-time">' + escapeHtml((c.created_at || '').substring(0, 16).replace('T', ' ')) + '</span>'
      + '</div>';
    var body = '<div class="cmt-body">' + escapeHtml(c.comment) + '</div>';
    var resolution = c.resolution_note
      ? '<div class="cmt-resolution"><strong>Resolved:</strong> ' + escapeHtml(c.resolution_note) + '</div>'
      : '';
    var thread = '';
    if (c.follow_up_thread && c.follow_up_thread.length) {
      thread = '<div class="cmt-thread">' + c.follow_up_thread.map(function(t) {
        return '<div class="cmt-thread-turn cmt-role-' + t.role + '">'
          + '<span class="cmt-thread-role">' + t.role + '</span>'
          + '<span class="cmt-thread-text">' + escapeHtml(t.text) + '</span>'
          + '</div>';
      }).join('') + '</div>';
    }
    var actions = '';
    if (c.status === 'open') {
      actions = '<div class="cmt-actions">'
        + '<button class="k-btn k-btn-quiet k-btn-sm" data-cmt-id="' + c.id + '" data-cmt-action="dismissed">Dismiss</button>'
        + '<button class="k-btn k-btn-quiet k-btn-sm" data-cmt-id="' + c.id + '" data-cmt-action="addressed">Mark addressed</button>'
        + '</div>';
    }
    return '<div class="cmt-card">' + head + body + resolution + thread + actions + '</div>';
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, function(ch) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch];
    });
  }

  // ---------------------------------------------------------------
  // Server I/O — POST new comment / update status. Falls back to
  // a warning + clipboard copy when the server isn't running.
  // ---------------------------------------------------------------
  function onSubmit(ev) {
    ev.preventDefault();
    if (!currentAnchor) return;
    var form = ev.target;
    var text = form.comment.value.trim();
    if (!text) return;
    var intent = form.intent.value || null;
    var payload = {
      ticker: TICKER,
      report_date: REPORT_DATE,
      anchor: currentAnchor,
      comment: text,
      intent: intent
    };
    // Snapshot the anchor at submit-time so a late-arriving response
    // clears the correct draft even if the user has since opened a
    // different anchor in the sidebar.
    var anchorAtSubmit = currentAnchor;
    fetch(SERVER_URL + '/comments', {
      method: 'POST',
      headers: MUTATION_HEADERS,
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json(); }).then(function(created) {
      commentStore.comments.push(created);
      form.reset();
      clearDraft(anchorAtSubmit);
      renderList();
      renderPins();
      hint('Posted.');
    }).catch(function(err) {
      // Server-down path: park the payload in the outbox so the timer
      // / focus / online flush will keep retrying without losing it.
      // Clear the draft + textarea so the user gets visual confirmation
      // the post is "in flight" — the Queued badge is the live status.
      enqueueOutbox(payload);
      clearDraft(anchorAtSubmit);
      form.reset();
      renderList();
      hint('Queued — will retry when server is back. (' + loadOutbox().length + ' total)');
      console.warn(err);
    });
  }

  // P4.5 "add note" capture: save the textarea straight into the analyst
  // journal (analyst_notes via /api/notes) anchored to the open section —
  // a durable thought, not a processor instruction.
  function onSaveNote() {
    if (!currentAnchor) return;
    var form = document.getElementById('cmt-form');
    var text = form.comment.value.trim();
    if (!text) { hint('Write the note text above first.'); return; }
    var kind = form.note_kind ? form.note_kind.value : 'observation';
    var anchorAtSubmit = currentAnchor;
    fetch(SERVER_URL + '/api/notes', {
      method: 'POST',
      headers: MUTATION_HEADERS,
      body: JSON.stringify({
        ticker: TICKER,
        kind: kind,
        body: text,
        anchor_type: anchorAtSubmit.type,
        anchor_key: anchorAtSubmit.key,
        fact_ref: anchorAtSubmit.fact_ref || null,
        context: {report_date: REPORT_DATE, tab: anchorAtSubmit.tab || null}
      })
    }).then(function(r) {
      if (!r.ok) throw new Error('notes HTTP ' + r.status);
      form.comment.value = '';
      clearDraft(anchorAtSubmit);
      hint('Saved to journal ✓');
    }).catch(function() {
      hint('Server unreachable — journal capture needs the research server.');
    });
  }

  function updateComment(id, status, btn) {
    CCAction.busy(btn);
    fetch(SERVER_URL + '/comments/' + id, {
      method: 'PATCH',
      headers: MUTATION_HEADERS,
      body: JSON.stringify({ticker: TICKER, report_date: REPORT_DATE, status: status})
    }).then(function(r) { return r.json(); }).then(function(updated) {
      for (var i = 0; i < commentStore.comments.length; i++) {
        if (commentStore.comments[i].id === id) commentStore.comments[i] = updated;
      }
      renderList();
      renderPins();
    }).catch(function() {
      CCAction.release(btn);
      hint('Server unreachable — cannot update.');
    });
  }

  function hint(msg) {
    var el = document.getElementById('cmt-form-hint');
    if (el) el.textContent = msg;
  }

  // ---------------------------------------------------------------
  // Free-text commenting (Google-Docs style)
  // ---------------------------------------------------------------
  var floater = null;
  function ensureFloater() {
    if (floater) return floater;
    floater = document.createElement('div');
    floater.className = 'cmt-floater';
    floater.style.display = 'none';
    floater.innerHTML = '<button type="button" class="cmt-floater-btn k-btn k-btn-primary k-btn-sm">+ Comment</button>';
    document.body.appendChild(floater);
    floater.querySelector('button').addEventListener('mousedown', function(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      onFloaterClick();
    });
    return floater;
  }
  function hideFloater() { if (floater) floater.style.display = 'none'; }

  function onSelectionChange() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed) return hideFloater();
    var text = (sel.toString() || '').trim();
    if (text.length < 2) return hideFloater();
    var node = sel.anchorNode;
    while (node && node !== document.body) {
      if (node.classList) {
        if (node.classList.contains('cmt-sidebar') ||
            node.classList.contains('cmt-floater') ||
            node.classList.contains('chat-drawer') ||
            node.classList.contains('chat-sidebar')) return hideFloater();
      }
      node = node.parentNode;
    }
    var range = sel.getRangeAt(0);
    var rect = range.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return hideFloater();
    ensureFloater();
    floater.style.display = 'block';
    floater.style.left = Math.round(rect.left + window.scrollX + rect.width / 2 - 56) + 'px';
    floater.style.top = Math.round(rect.bottom + window.scrollY + 6) + 'px';
  }

  function onFloaterClick() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed) return hideFloater();
    var text = (sel.toString() || '').trim();
    if (!text) return hideFloater();
    var anchorNode = sel.anchorNode;
    var anchorEl = (anchorNode && anchorNode.nodeType === 1) ? anchorNode
      : (anchorNode && anchorNode.parentElement) || document.body;
    var landmark = findLandmark(anchorEl);
    var occurrence = countOccurrencesBefore(landmark.scope, text, sel.getRangeAt(0));
    var tabAttr = anchorEl.closest ? anchorEl.closest('[data-tab]') : null;
    var anchor = {
      type: 'free_text',
      key: text.substring(0, 200),
      tab: tabAttr ? tabAttr.getAttribute('data-tab') : null,
      parent_landmark: landmark.label,
      occurrence_index: occurrence
    };
    hideFloater();
    openSidebarForAnchor(anchor);
  }

  function findLandmark(el) {
    var cur = el;
    while (cur && cur !== document.body) {
      if (cur.classList && cur.classList.contains('panel')) {
        var title = cur.querySelector(':scope > .panel-head .panel-title');
        var t = (title && title.textContent || '').trim();
        if (t) return {label: 'panel: ' + t, scope: cur};
      }
      if (cur.classList && cur.classList.contains('tab-body')) {
        var tab = cur.closest('[data-tab]');
        var tabName = (tab && tab.getAttribute('data-tab')) || 'unknown';
        return {label: 'tab: ' + tabName, scope: cur};
      }
      cur = cur.parentNode;
    }
    return {label: 'document', scope: document.body};
  }

  function countOccurrencesBefore(scope, needle, range) {
    var pre = range.cloneRange();
    pre.selectNodeContents(scope);
    pre.setEnd(range.startContainer, range.startOffset);
    var before = pre.toString();
    var count = 0;
    var seek = 0;
    while ((seek = before.indexOf(needle, seek)) !== -1) {
      count++;
      seek += needle.length;
    }
    return count;
  }

  function openSidebarForAnchor(anchor) {
    var label = anchor.type === 'free_text'
      ? ((anchor.parent_landmark || 'document') + ' · "' +
         anchor.key.substring(0, 60) + (anchor.key.length > 60 ? '…' : '') + '"')
      : humanAnchor(anchor);
    openWithAnchor(anchor, label);
  }

  function renderFreeTextHighlights() {
    document.querySelectorAll('mark.cmt-highlight').forEach(function(m) {
      var parent = m.parentNode; if (!parent) return;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize();
    });
    var freeText = commentStore.comments.filter(function(c) {
      return c.anchor && c.anchor.type === 'free_text';
    });
    freeText.forEach(highlightFreeText);
  }

  function highlightFreeText(c) {
    var scope = locateLandmarkScope(c.anchor.parent_landmark || '', c.anchor.tab);
    if (!scope) return;
    var ranges = findTextRanges(scope, c.anchor.key);
    if (ranges.length === 0) return;
    var pick = ranges[Math.min(c.anchor.occurrence_index || 0, ranges.length - 1)];
    var mark = document.createElement('mark');
    mark.className = 'cmt-highlight';
    if (c.status !== 'open') mark.classList.add('addressed');
    mark.setAttribute('data-cmt-id', c.id);
    mark.setAttribute('title', 'Comment · click to view');
    try { pick.surroundContents(mark); } catch (_) { return; }
    mark.addEventListener('click', function(ev) {
      ev.stopPropagation();
      openSidebarForAnchor(c.anchor);
    });
  }

  function locateLandmarkScope(label, tab) {
    if (!label) return document.body;
    if (label.indexOf('panel: ') === 0) {
      var title = label.substring(7);
      var panels = document.querySelectorAll('.panel');
      for (var i = 0; i < panels.length; i++) {
        var t = panels[i].querySelector(':scope > .panel-head .panel-title');
        if (t && (t.textContent || '').trim() === title) return panels[i];
      }
      return null;
    }
    if (label.indexOf('tab: ') === 0) {
      var name = label.substring(5);
      var pane = document.querySelector('[data-tab="' + name + '"].tab-pane');
      return pane || null;
    }
    if (tab) {
      var fallback = document.querySelector('[data-tab="' + tab + '"].tab-pane');
      if (fallback) return fallback;
    }
    return document.body;
  }

  function findTextRanges(scope, needle) {
    var out = [];
    var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, null);
    while (walker.nextNode()) {
      var node = walker.currentNode;
      var text = node.nodeValue;
      var pos = 0;
      while ((pos = text.indexOf(needle, pos)) !== -1) {
        var r = document.createRange();
        r.setStart(node, pos);
        r.setEnd(node, pos + needle.length);
        out.push(r);
        pos += needle.length;
      }
    }
    return out;
  }

  // ---------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------
  function bootAll() {
    renderPins();
    renderFreeTextHighlights();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootAll);
  } else {
    bootAll();
  }
  document.addEventListener('click', function(ev) {
    if (ev.target && ev.target.matches('.tab')) {
      setTimeout(bootAll, 0);
    }
  });
  document.addEventListener('mouseup', function() {
    setTimeout(onSelectionChange, 0);
  });
  document.addEventListener('selectionchange', function() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed) hideFloater();
  });
  document.addEventListener('scroll', hideFloater, true);
  // The selection floater is a transient popover — Escape-only via CCOverlay's
  // one keydown (no second listener, no scrim/trap). It claims Escape first
  // (innermost layer); a further Escape then closes the sidebar through its
  // CCOverlay registration. The sidebar's own close (x) stays wired to closeSidebar.
  if (window.CCOverlay) {
    window.CCOverlay.addPopoverDismisser(function() {
      if (floater && floater.style.display !== 'none') { hideFloater(); return true; }
      return false;
    });
  }

  // Re-render highlights after a successful POST so new free_text
  // comments light up without a page reload.
  var origRenderPins = renderPins;
  renderPins = function() {
    origRenderPins();
    renderFreeTextHighlights();
  };
})();


(function () {
  if (window.ccCiteMarks) return;
  // A [n] marker, optionally preceded by a financial value (currency-led, or a
  // bare number that carries a %/magnitude suffix — so plain years/counts are
  // NOT highlighted), where the value may be wrapped in one inline tag. The
  // Python mirror (ui.cite_marks._CITE_RX) matches the same shape verbatim.
  var CITE_RX = /(?:((?:<(?:strong|em|code)>)?(?:[$€£]\s?\d[\d,]*(?:\.\d+)?\s?(?:%|bps|pp|x|[BMK]|bn|mn|tn|billion|million|trillion|thousand)?|\d[\d,]*(?:\.\d+)?\s?(?:%|bps|pp|x|[BMK]|bn|mn|tn|billion|million|trillion|thousand))(?:<\/(?:strong|em|code)>)?)\s*)?\[(\d{1,2})\]/g;
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function popHtml(c) {
    var head = '';
    var label;
    if (c.ticker) {
      head = '<span class="cite-pop-head"><span class="cite-pop-tick">' + esc(c.ticker) + '</span>';
      if (c.doc_type) head += '<span class="cite-pop-kind">' + esc(c.doc_type) + '</span>';
      if (c.period) head += '<span class="cite-pop-per">' + esc(c.period) + '</span>';
      head += '</span>';
      label = String(c.label || '');
      var pfx = c.ticker + ' · ';
      if (label.indexOf(pfx) === 0) label = label.slice(pfx.length);
    } else {
      label = String(c.label || 'source');
    }
    var html = head;
    if (label) html += '<span class="cite-pop-label">' + esc(label) + '</span>';
    if (c.value) {
      html += '<span class="cite-pop-value">' + esc(c.value) + '</span>'
        + '<span class="cite-pop-value-cap">latest reported</span>';
    }
    var meta = [];
    if (c.kind && !c.ticker) meta.push(esc(c.kind));
    if (typeof c.confidence === 'number') {
      meta.push('confidence ' + Math.round(c.confidence * 100) + '%');
    }
    if (meta.length) html += '<span class="cite-pop-meta">' + meta.join(' &middot; ') + '</span>';
    return '<span class="cite-pop" role="tooltip">' + html + '</span>';
  }
  function safeHref(raw, base) {
    var href = String(raw || '').trim();
    if (!href || /[\u0000-\u001f\u007f]/.test(href)) return '';
    if (/^https?:\/\//i.test(href)) {
      try {
        var absolute = new URL(href);
        if ((absolute.protocol === 'http:' || absolute.protocol === 'https:')
            && absolute.hostname && !absolute.username && !absolute.password) return href;
      } catch (_err) {}
      return '';
    }
    if (href.charAt(0) !== '/' || href.charAt(1) === '/' || href.indexOf('\\') !== -1) return '';
    if (!base) return href;
    var safeBase = safeHref(base, '');
    return safeBase ? safeBase.replace(/\/$/, '') + href : '';
  }
  function linkify(html, items, opts) {
    var base = (opts && opts.hrefBase) || '';
    var map = {};
    (items || []).forEach(function (c) { if (c && c.n) map[String(c.n)] = c; });
    return String(html).replace(CITE_RX, function (m, value, n) {
      var c = map[n];
      if (!c) return m;
      var href = safeHref(c.href || c.source_url || '', base);
      var pop = popHtml(c);
      if (value) {
        var badge = href
          ? '<a class="cite-mark cite-badge" href="' + esc(href) + '" target="_blank" rel="noopener">' + n + '</a>'
          : '<span class="cite-mark cite-badge">' + n + '</span>';
        return '<span class="cite-wrap" tabindex="0"><span class="cite-val">' + value + '</span>'
          + badge + pop + '</span>';
      }
      var mark = href
        ? '<a class="cite-mark" href="' + esc(href) + '" target="_blank" rel="noopener">[' + n + ']</a>'
        : '<span class="cite-mark">[' + n + ']</span>';
      return '<span class="cite-wrap" tabindex="0">' + mark + pop + '</span>';
    });
  }
  function unverifiedChipHtml(claims) {
    var bad = (claims || []).filter(function (c) { return c && c.supported === false; });
    if (!bad.length) return '';
    var titles = bad.map(function (c) { return c.text || ''; }).filter(Boolean).join('\n');
    return '<span class="cite-unverified" title="' + esc(titles) + '">&#9888; '
      + bad.length + ' unverified claim' + (bad.length === 1 ? '' : 's') + '</span>';
  }
  window.ccCiteMarks = { linkify: linkify, unverifiedChipHtml: unverifiedChipHtml };
  // Escape-only dismissal (Law 3 / design_language §3.1): a cite popover is
  // phrasing content revealed on :focus-within — NOT a modal, so it must not
  // gain a scrim or focus trap. Register a CCOverlay dismisser that blurs the
  // focused .cite-wrap; the :hover variant just leaves on mouseout. Runs once
  // per document (the ccCiteMarks guard above), and only when CCOverlay is
  // present (e.g. the shell + the report iframe).
  if (window.CCOverlay) {
    window.CCOverlay.addPopoverDismisser(function () {
      var ae = document.activeElement;
      if (ae && ae.closest && ae.closest('.cite-wrap')) { ae.blur(); return true; }
      return false;
    });
  }
})();

(function () {
  'use strict';

  function init() {
    var boot = window.__workspaceCommentBoot;
    if (!boot) {
      window.setTimeout(init, 100);
      return;
    }
    var SERVER_URL = /^https?:$/.test(window.location.protocol)
      ? window.location.origin
      : (boot.server_url || 'http://localhost:7421');
    var TICKER = String(boot.ticker || '').toUpperCase();
    var REPORT_DATE = String(boot.report_date || '');
    var sidebar = document.getElementById('chat-sidebar');
    var toggle = document.getElementById('chat-toggle');
    var handoff = document.getElementById('chat-open-copilot');
    if (!sidebar || !toggle || !handoff) return;

    var launchQuery = new URLSearchParams({
      copilot: '1', ticker: TICKER, report_date: REPORT_DATE,
      origin_key: 'report:' + TICKER + ':' + REPORT_DATE
    });
    var launchUrl = SERVER_URL + '/?' + launchQuery.toString() + '#screen-workspace';
    handoff.href = launchUrl;
    handoff.textContent = 'Open in Copilot';

    function openDurableCopilot(context) {
      var payload = Object.assign({
        company_ticker: TICKER,
        category: 'research',
        report_date: REPORT_DATE,
        origin_key: 'report:' + TICKER + ':' + REPORT_DATE,
        coverage_role_at_creation: 'unknown',
        lifecycle_at_creation: 'unknown'
      }, context || {});
      try {
        if (window.parent !== window && typeof window.parent.openWorkOsCopilot === 'function') {
          window.parent.openWorkOsCopilot(payload);
          return true;
        }
      } catch (_) { /* cross-origin embeds use the clear Work OS link */ }
      if (typeof window.openWorkOsCopilot === 'function') {
        window.openWorkOsCopilot(payload);
        return true;
      }
      return false;
    }

    function applyOpen(open) {
      sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
      sidebar.classList.toggle('open', open);
      toggle.classList.toggle('open', open);
      if (open) document.documentElement.style.setProperty('--sidebar-open-width', 'var(--sidebar-width)');
      else document.documentElement.style.removeProperty('--sidebar-open-width');
    }

    var chatOv = window.CCOverlay && window.CCOverlay.register(sidebar, {
      modal: true, priority: 30, scrim: false, trapFocus: false, restoreFocus: true,
      motion: 'none', toggleHidden: false, autofocus: false,
      group: 'report-sidebar', closeId: 'chat-close', wireClose: false,
      onOpen: function () { applyOpen(true); },
      onClose: function () { applyOpen(false); }
    });
    function setOpen(open) {
      if (!chatOv) { applyOpen(open); return; }
      if (open) chatOv.open(); else chatOv.close();
    }

    var embedded = false;
    try { embedded = window.self !== window.top; } catch (_) { embedded = true; }
    if (embedded) {
      var launcher = document.getElementById('chat-drawer');
      if (launcher) launcher.hidden = true;
      handoff.target = '_top';
    }

    toggle.addEventListener('click', function () {
      if (!openDurableCopilot({})) setOpen(sidebar.getAttribute('aria-hidden') === 'true');
    });
    sidebar.querySelector('.chat-close').addEventListener('click', function () { setOpen(false); });
    handoff.addEventListener('click', function (event) {
      if (!openDurableCopilot({})) return;
      event.preventDefault();
      setOpen(false);
    });

    document.addEventListener('click', function (event) {
      var doorway = event.target.closest && event.target.closest('.fact-doorway');
      if (!doorway) return;
      var host = doorway.closest('[data-fact-ref]');
      var factRef = host && host.getAttribute('data-fact-ref');
      if (!factRef) return;
      var label = (doorway.textContent || '').replace(/\s+/g, ' ').trim();
      if (openDurableCopilot({
        fact_ref: factRef,
        prompt: label ? ('Review ' + label + ' with its governed evidence.') : 'Review this governed fact.'
      })) event.preventDefault();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();


(function () {
  var root = document.getElementById('dcf-edit');
  if (!root) return;
  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  var boot = readJson('workspace-boot') || {};
  var SERVER_URL = /^https?:$/.test(window.location.protocol)
    ? window.location.origin
    : (boot.server_url || 'http://localhost:7421');
  var MUTATION_HEADERS = window.__workspaceMutationHeaders || {'Content-Type': 'application/json'};
  var TICKER = root.getAttribute('data-dcf-ticker') || boot.ticker;

  var elToggle = document.getElementById('dcf-edit-toggle');
  var elBody = document.getElementById('dcf-edit-body');
  var elStatus = document.getElementById('dcf-edit-status');
  var elControls = document.getElementById('dcf-edit-controls');
  var elScenarios = document.getElementById('dcf-edit-scenarios');
  var elHeatmap = document.getElementById('dcf-edit-heatmap');
  var elReset = document.getElementById('dcf-edit-reset');
  var elSave = document.getElementById('dcf-edit-save');

  var loaded = null;   // canonical inputs as last fetched / saved
  var model = null;    // working copy with live edits
  var ready = false;
  var debounceTimer = null;

  // Rate-like fields edit as percent (x100); the rest are raw numbers.
  var SCALARS = [
    {key: 'wacc', label: 'WACC', pct: true, step: 0.1},
    {key: 'near_op_margin', label: 'Near op margin', pct: true, step: 0.5},
    {key: 'terminal_op_margin', label: 'Term op margin', pct: true, step: 0.5},
    {key: 'exit_multiple', label: 'Exit multiple', pct: false, step: 0.5},
    {key: 'terminal_growth_g', label: 'Terminal g', pct: true, step: 0.1},
    {key: 'tax_rate', label: 'Tax rate', pct: true, step: 0.5}
  ];
  var DRIVERS = [
    {key: 'beta', label: 'Beta', pct: false, step: 0.05},
    {key: 'risk_free_rate', label: 'Risk-free', pct: true, step: 0.1},
    {key: 'equity_risk_premium', label: 'ERP', pct: true, step: 0.1},
    {key: 'cost_of_debt', label: 'Cost of debt', pct: true, step: 0.1}
  ];
  var SPEC_BY_KEY = {};
  SCALARS.concat(DRIVERS).forEach(function (s) { SPEC_BY_KEY[s.key] = s; });
  var inputsByKey = {};   // key -> <input>, refreshed by buildControls (Wave 5)

  function setStatus(msg, tone) {
    elStatus.textContent = msg || '';
    elStatus.className = 'dcf-edit-status' + (tone ? ' is-' + tone : '');
  }
  function fmtMoney(x) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return '$' + Number(x).toFixed(2);
  }
  function fmtPct(x) { return (Number(x) * 100).toFixed(1) + '%'; }
  function fmtMult(x) { return Number(x).toFixed(1) + 'x'; }

  // The CAPM derivation, identical to redesign.read_inputs: editing a driver
  // re-derives WACC so the preview stays consistent (a direct WACC edit is a
  // preview-only override that the durable save expresses via the drivers).
  function deriveWacc(m) {
    var ke = m.risk_free_rate + m.beta * m.equity_risk_premium;
    var akd = m.cost_of_debt * (1 - m.tax_rate);
    var mcap = m.current_price * m.diluted_shares_m;
    var denom = mcap + m.total_debt_m;
    var ew = denom > 0 ? mcap / denom : 1.0;
    return ew * ke + (1 - ew) * akd;
  }

  function numField(spec, value, onChange) {
    var wrap = document.createElement('div');
    wrap.className = 'dcf-edit-field';
    var lab = document.createElement('label');
    lab.textContent = spec.label + (spec.pct ? ' (%)' : '');
    var inp = document.createElement('input');
    inp.type = 'number';
    inp.step = String(spec.step);
    inp.value = spec.pct ? (Number(value) * 100).toFixed(2) : String(value);
    inp.addEventListener('input', function () {
      var raw = parseFloat(inp.value);
      if (isNaN(raw)) return;
      onChange(spec.pct ? raw / 100 : raw);
    });
    wrap.appendChild(lab);
    wrap.appendChild(inp);
    return {wrap: wrap, input: inp};
  }

  function group(title) {
    var g = document.createElement('div');
    g.className = 'dcf-edit-group';
    var t = document.createElement('div');
    t.className = 'dcf-edit-group-title';
    t.textContent = title;
    g.appendChild(t);
    return g;
  }

  var waccInput = null;  // kept so driver edits can refresh the WACC display

  function buildControls() {
    elControls.textContent = '';

    // Terminal + valuation levers.
    var gVal = group('Terminal & valuation');
    var methodWrap = document.createElement('div');
    methodWrap.className = 'dcf-edit-field';
    var mlab = document.createElement('label');
    mlab.textContent = 'Terminal method';
    var sel = document.createElement('select');
    ['Exit multiple', 'Perpetuity'].forEach(function (opt) {
      var o = document.createElement('option');
      o.value = opt; o.textContent = opt;
      if (model.terminal_method === opt) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener('change', function () {
      model.terminal_method = sel.value;
      scheduleRecompute();
    });
    methodWrap.appendChild(mlab);
    methodWrap.appendChild(sel);
    var fieldsVal = document.createElement('div');
    fieldsVal.className = 'dcf-edit-fields';
    fieldsVal.appendChild(methodWrap);
    SCALARS.forEach(function (spec) {
      var f = numField(spec, model[spec.key], function (v) {
        model[spec.key] = v;
        scheduleRecompute();
      });
      if (spec.key === 'wacc') waccInput = f.input;
      inputsByKey[spec.key] = f.input;
      fieldsVal.appendChild(f.wrap);
    });
    gVal.appendChild(fieldsVal);
    elControls.appendChild(gVal);

    // CAPM drivers — editing one re-derives WACC (durable path).
    var gCapm = group('WACC drivers (re-derive WACC)');
    var fieldsCapm = document.createElement('div');
    fieldsCapm.className = 'dcf-edit-fields';
    DRIVERS.forEach(function (spec) {
      var f = numField(spec, model[spec.key], function (v) {
        model[spec.key] = v;
        model.wacc = deriveWacc(model);
        if (waccInput) waccInput.value = (model.wacc * 100).toFixed(2);
        scheduleRecompute();
      });
      inputsByKey[spec.key] = f.input;
      fieldsCapm.appendChild(f.wrap);
    });
    gCapm.appendChild(fieldsCapm);
    elControls.appendChild(gCapm);

    // Per-segment growth.
    var segs = model.segments || [];
    if (segs.length) {
      var gSeg = group('Segment growth (near / terminal)');
      var grid = document.createElement('div');
      grid.className = 'dcf-seg-grid';
      var h0 = document.createElement('div'); h0.className = 'dcf-seg-head'; h0.textContent = '';
      var h1 = document.createElement('div'); h1.className = 'dcf-seg-head'; h1.textContent = 'near %';
      var h2 = document.createElement('div'); h2.className = 'dcf-seg-head'; h2.textContent = 'term %';
      grid.appendChild(h0); grid.appendChild(h1); grid.appendChild(h2);
      segs.forEach(function (name) {
        var nm = document.createElement('div');
        nm.className = 'dcf-seg-name'; nm.textContent = name; nm.title = name;
        grid.appendChild(nm);
        grid.appendChild(segInput(model.near_growth_by_segment, name));
        grid.appendChild(segInput(model.terminal_growth_by_segment, name));
      });
      gSeg.appendChild(grid);
      elControls.appendChild(gSeg);
    }
  }

  function segInput(mapRef, name) {
    var inp = document.createElement('input');
    inp.type = 'number'; inp.step = '0.5';
    inp.value = (Number(mapRef[name]) * 100).toFixed(2);
    inp.addEventListener('input', function () {
      var raw = parseFloat(inp.value);
      if (isNaN(raw)) return;
      mapRef[name] = raw / 100;
      scheduleRecompute();
    });
    return inp;
  }

  function renderScenarios(data) {
    elScenarios.textContent = '';
    var price = data.current_price;
    var sc = data.scenarios || {};
    [['bear', 'Bear'], ['base', 'Base'], ['bull', 'Bull']].forEach(function (pair) {
      var key = pair[0];
      var cell = document.createElement('div');
      cell.className = 'dcf-scn' + (key === 'base' ? ' base' : '');
      var lab = document.createElement('div');
      lab.className = 'dcf-scn-label'; lab.textContent = pair[1];
      var val = document.createElement('div');
      val.className = 'dcf-scn-val'; val.textContent = fmtMoney(sc[key]);
      var up = document.createElement('div');
      var fv = sc[key];
      if (fv !== null && fv !== undefined && price) {
        var pct = (fv - price) / price * 100;
        up.className = 'dcf-scn-up ' + (pct >= 0 ? 'pos' : 'neg');
        up.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%';
      } else {
        up.className = 'dcf-scn-up muted'; up.textContent = '—';
      }
      cell.appendChild(lab); cell.appendChild(val); cell.appendChild(up);
      elScenarios.appendChild(cell);
    });
  }

  function renderHeatmap(sens) {
    elHeatmap.textContent = '';
    if (!sens || !sens.values) return;
    var price = sens.current_price || 0;
    var cap = document.createElement('div');
    cap.className = 'dcf-hm-cap';
    cap.textContent = 'Fair value / share - exit multiple (rows) x WACC (cols); '
      + 'green above price';
    elHeatmap.appendChild(cap);
    var tbl = document.createElement('table');
    tbl.className = 'dcf-hm-table';
    var thead = document.createElement('thead');
    var hr = document.createElement('tr');
    var corner = document.createElement('th');
    corner.className = 'dcf-hm-axis'; corner.textContent = 'mult \\ WACC';
    hr.appendChild(corner);
    sens.wacc_axis.forEach(function (w) {
      var th = document.createElement('th');
      th.textContent = fmtPct(w);
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    tbl.appendChild(thead);
    var tbody = document.createElement('tbody');
    var mid = Math.floor(sens.values.length / 2);
    sens.values.forEach(function (row, i) {
      var tr = document.createElement('tr');
      var rh = document.createElement('th');
      rh.textContent = fmtMult(sens.multiple_axis[i]);
      tr.appendChild(rh);
      row.forEach(function (v, j) {
        var td = document.createElement('td');
        td.textContent = fmtMoney(v);
        var rel = price > 0 ? (v - price) / price : 0;
        var mag = Math.min(1, Math.abs(rel) / 0.5);
        var tone = rel >= 0 ? 'var(--ok)' : 'var(--bad)';
        var tint = Math.round(8 + mag * 30);
        td.style.background = 'color-mix(in srgb, ' + tone + ' ' + tint + '%, transparent)';
        td.title = fmtMult(sens.multiple_axis[i]) + ' · ' + fmtPct(sens.wacc_axis[j])
          + ' → ' + fmtMoney(v);
        if (i === mid && j === mid) td.className = 'base';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    elHeatmap.appendChild(tbl);
  }

  function recompute() {
    if (!ready) return;
    setStatus('Recomputing…');
    fetch(SERVER_URL + '/api/dcf/recompute', {
      method: 'POST',
      headers: MUTATION_HEADERS,
      body: JSON.stringify({inputs: model})
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    }).then(function (res) {
      if (!res.ok) {
        setStatus((res.body && res.body.error) || ('recompute failed (' + res.status + ')'), 'bad');
        return;
      }
      renderScenarios(res.body);
      renderHeatmap(res.body.sensitivity);
      var ou = res.body.over_under_pct;
      if (ou !== null && ou !== undefined) {
        var pct = (ou * 100);
        setStatus('Base ' + fmtMoney(res.body.fair_value_per_share_usd) + ' · '
          + (pct >= 0 ? 'over' : 'under') + ' by ' + Math.abs(pct).toFixed(0)
          + '% vs price · WACC ' + fmtPct(res.body.wacc), '');
      } else {
        setStatus('Base ' + fmtMoney(res.body.fair_value_per_share_usd)
          + ' · WACC ' + fmtPct(res.body.wacc), '');
      }
    }).catch(function () {
      setStatus('Research server offline — start comments_server to recompute.', 'bad');
    });
  }

  function scheduleRecompute() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(recompute, 280);
  }

  function load() {
    setStatus('Loading model…');
    fetch(SERVER_URL + '/api/dcf/inputs/' + encodeURIComponent(TICKER))
      .then(function (r) {
        if (r.status === 404) { setStatus('No editable DCF model for this ticker.', ''); return null; }
        return r.json().then(function (j) { return {ok: r.ok, body: j}; });
      }).then(function (res) {
        if (!res) return;
        if (!res.ok || !res.body || !res.body.inputs) {
          setStatus((res.body && res.body.error) || 'Could not load the model.', 'bad');
          return;
        }
        loaded = res.body.inputs;
        model = JSON.parse(JSON.stringify(loaded));
        ready = true;
        buildControls();
        recompute();
        if (pendingInject) {
          var pi = pendingInject; pendingInject = null;
          applyInject(pi.key, pi.value, pi.label);
        }
      }).catch(function () {
        setStatus('Research server offline — start comments_server to edit.', 'bad');
      });
  }

  // --- Wave 5: KPI -> DCF driver injection ---------------------------------
  // A captured report value carries a "-> DCF" affordance
  // [data-dcf-inject=key data-dcf-value=<model units> data-dcf-label]. Clicking
  // it opens the editor, sets that input (re-deriving WACC for a CAPM driver),
  // and recomputes. If the editor hasn't loaded, the inject is queued and
  // applied once the model arrives.
  var pendingInject = null;
  function applyInject(key, value, label) {
    if (!model || !(key in model)) { setStatus('No DCF input "' + key + '".', 'bad'); return; }
    model[key] = value;
    if (DRIVERS.some(function (d) { return d.key === key; })) {
      model.wacc = deriveWacc(model);
      if (inputsByKey.wacc) inputsByKey.wacc.value = (model.wacc * 100).toFixed(2);
    }
    var inp = inputsByKey[key], spec = SPEC_BY_KEY[key];
    if (inp && spec) {
      inp.value = spec.pct ? (value * 100).toFixed(2) : String(value);
      inp.classList.add('dcf-injected');
      setTimeout(function () { inp.classList.remove('dcf-injected'); }, 1500);
    }
    setStatus('Injected ' + (label || key) + ' — recomputing…', 'ok');
    scheduleRecompute();
  }
  window.dcfSetDriver = function (key, value, label) {
    if (isNaN(value)) return;
    if (elBody.hidden) { elBody.hidden = false; elToggle.setAttribute('aria-expanded', 'true'); }
    root.scrollIntoView({behavior: 'smooth', block: 'center'});
    if (ready && model) {
      applyInject(key, value, label);
    } else {
      pendingInject = {key: key, value: value, label: label};
      if (loaded === null) load();
      else setStatus('Loading model to inject ' + (label || key) + '…', 'warn');
    }
  };
  document.addEventListener('click', function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest('[data-dcf-inject]') : null;
    if (!a) return;
    ev.preventDefault();
    window.dcfSetDriver(
      a.getAttribute('data-dcf-inject'),
      parseFloat(a.getAttribute('data-dcf-value')),
      a.getAttribute('data-dcf-label') || ''
    );
  });

  elToggle.addEventListener('click', function () {
    var open = elBody.hidden;
    elBody.hidden = !open;
    elToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !ready && loaded === null) load();
  });

  elReset.addEventListener('click', function () {
    if (!loaded) return;
    model = JSON.parse(JSON.stringify(loaded));
    buildControls();
    recompute();
  });

  elSave.addEventListener('click', function () {
    if (!ready) return;
    CCAction.busy(elSave, 'Saving…');
    setStatus('Saving…');
    fetch(SERVER_URL + '/api/dcf/save', {
      method: 'POST',
      headers: MUTATION_HEADERS,
      body: JSON.stringify({ticker: TICKER, inputs: model})
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    }).then(function (res) {
      if (!res.ok) {
        CCAction.release(elSave);
        setStatus((res.body && res.body.error) || ('save failed (' + res.status + ')'), 'bad');
        return;
      }
      // Adopt the canonical saved inputs (WACC re-derived from saved drivers) as
      // the new reset baseline, then re-render from the persisted state.
      if (res.body.inputs) {
        loaded = res.body.inputs;
        model = JSON.parse(JSON.stringify(loaded));
        buildControls();
      }
      if (res.body.sensitivity) { renderScenarios(res.body); renderHeatmap(res.body.sensitivity); }
      CCAction.receipt(elSave, '✓ Saved');
      setStatus('Saved to model ✓ · override ledger updated (Opus baseline untouched).', 'ok');
      // Saving again after further slider adjustments is the normal flow —
      // unlock once the receipt has registered rather than staying terminal.
      setTimeout(function () { CCAction.release(elSave); }, 1500);
    }).catch(function () {
      CCAction.release(elSave);
      setStatus('Research server offline — could not save.', 'bad');
    });
  });
})();


(function() {
  function init() {
    var boot = window.__workspaceCommentBoot;
    if (!boot) {
      setTimeout(init, 100);
      return;
    }
    var SERVER_URL = /^https?:$/.test(window.location.protocol)
      ? window.location.origin
      : (boot.server_url || 'http://localhost:7421');
    var MUTATION_HEADERS = window.__workspaceMutationHeaders || {'Content-Type': 'application/json'};

    document.querySelectorAll('.l1-decision-card .dc-act').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var verb = btn.getAttribute('data-verb');
        var artifactId = btn.getAttribute('data-artifact-id');
        var card = btn.closest('.l1-decision-card');
        var statusEl = card ? card.querySelector('.dc-status') : null;
        if (!artifactId || artifactId === '0') {
          if (statusEl) statusEl.textContent = 'No artifact id — cannot record a disposition.';
          return;
        }
        var actions = card ? card.querySelectorAll('.dc-act') : [btn];
        CCAction.busy(btn, 'Recording…');
        actions.forEach(function (b) { if (b !== btn) b.disabled = true; });
        if (statusEl) statusEl.textContent = 'Recording ' + verb + '…';
        fetch(SERVER_URL + '/api/research/card/' + artifactId + '/' + verb, {
          method: 'POST',
          headers: MUTATION_HEADERS,
          body: JSON.stringify({})
        }).then(function (r) {
          return r.json().then(function (data) { return {ok: r.ok, data: data}; });
        }).then(function (res) {
          actions.forEach(function (b) { if (b !== btn) b.disabled = false; });
          if (!res.ok) {
            CCAction.release(btn);
            if (statusEl) statusEl.textContent = 'Failed: ' + (res.data.error || 'server error');
            return;
          }
          CCAction.receipt(btn, '✓ ' + verb);
          if (statusEl) statusEl.textContent = 'Recorded: ' + res.data.status;
        }).catch(function (err) {
          actions.forEach(function (b) { if (b !== btn) b.disabled = false; });
          CCAction.release(btn);
          if (statusEl) statusEl.textContent = 'Server unreachable — run start_comments_server.bat.';
          console.warn(err);
        });
      });
    });
  }
  init();
})();
