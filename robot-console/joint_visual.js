/* Pure calibrated kinematics; importing this module never connects or moves. */
const JointVisual = (() => {
 const I=()=>[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
 const mul=(a,b)=>Array.from({length:16},(_,n)=>{const r=n>>2,c=n%4;return [0,1,2,3].reduce((s,k)=>s+a[r*4+k]*b[k*4+c],0)});
 function rot(axis,q){let [x,y,z]=axis,n=Math.hypot(x,y,z);if(!n||!Number.isFinite(q))throw Error('Invalid joint geometry');x/=n;y/=n;z/=n;const c=Math.cos(q),s=Math.sin(q),v=1-c;return [x*x*v+c,x*y*v-z*s,x*z*v+y*s,0,y*x*v+z*s,y*y*v+c,y*z*v-x*s,0,z*x*v-y*s,z*y*v+x*s,z*z*v+c,0,0,0,0,1]}
 function origin(j){const [r,p,y]=j.rpy,m=mul(mul(rot([0,0,1],y),rot([0,1,0],p)),rot([1,0,0],r));[m[3],m[7],m[11]]=j.xyz;return m}
 function solve(model,states,override={}){const by=Object.fromEntries(states.map(j=>[j.name,j])),links={torso_base_link:I()},out=[];for(const j of model){const parent=links[j.parent];if(!parent)continue;let q=0;if(j.type!=='fixed'){const s=by[j.name];if(!s?.fresh||!Number.isFinite(s.position))continue;q=override[j.name]??s.position;if(!Number.isFinite(q))continue}const pre=mul(parent,origin(j)),m=j.type==='fixed'?pre:mul(pre,rot(j.axis,q));links[j.child]=m;const a=j.axis;out.push({...j,p:[pre[3],pre[7],pre[11]],end:[m[3],m[7],m[11]],parentP:[parent[3],parent[7],parent[11]],axisWorld:[0,1,2].map(r=>[0,1,2].reduce((s,k)=>s+pre[r*4+k]*a[k],0))})}return out}
 function label(name){const m=name.match(/^(left|right)_arm_joint([1-7])$/);if(!m)return name;return `${m[1]==='right'?'右':'左'}${['肩部 · 靠躯干','肩部 · 第二轴','上臂 · 第三轴','肘部','前臂','腕部 · 第六轴','手端 · 第七轴'][+m[2]-1]} J${m[2]}`}
 function targetDelta(j,degrees){if(!j?.fresh||!j.controllable||!Number.isFinite(degrees)||!j.limits||degrees*Math.PI/180<j.limits[0]-1e-10||degrees*Math.PI/180>j.limits[1]+1e-10)throw Error('目标超出模型限位或反馈不可用');return degrees-j.position*180/Math.PI}
 return {solve,label,targetDelta};
})();
if(typeof module!=='undefined')module.exports=JointVisual;
