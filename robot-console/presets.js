/* Pure plan builder: validates every waypoint before any command is sent. */
(function(root){
function plan(kind,joints,side,amplitude,saved){
 const by=Object.fromEntries(joints.map(j=>[j.name,j]));const steps=[];
 function add(name,target){const j=by[name];if(!j||!j.fresh)throw Error('关节状态不可用：'+name);if(!Number.isFinite(target)||!j.limits||target<j.limits[0]||target>j.limits[1])throw Error('预设超出关节限位：'+name);steps.push({name,target})}
 if(!Number.isFinite(amplitude)||amplitude<1||amplitude>15)throw Error('动作幅度须为 1–15°');
 if(!['left_arm','right_arm'].includes(side))throw Error('请选择手臂');
 const a=amplitude*Math.PI/180;
 if(kind==='nod'){const j=by.head_joint2;if(!j)throw Error('头部状态不可用');const offset=j.position+a<=j.limits[1]?a:-a;add(j.name,j.position+offset);add(j.name,j.position)}
 else if(kind==='wave'){const j=by[side+'_joint7'];if(!j)throw Error('手腕状态不可用');for(const offset of [a,-a,a,-a,0])add(j.name,j.position+offset)}
 else if(kind==='raise'){if(!saved||saved.side!==side||!Array.isArray(saved.joints)||saved.joints.length!==7)throw Error('尚未识别到稳定抬臂姿态，请等待实时识别');for(let i=1;i<=7;i++){const name=side+'_joint'+i;const j=saved.joints.find(x=>x.name===name);if(!j)throw Error('自动识别的姿态不完整');add(name,j.position)}}
 else throw Error('未知预设');return steps;
}
class AutoRaiseTracker {
 constructor(){this.targets={};this.candidates={}}
 update(joints,posture,now,paused=false){
  for(const side of ['left_arm','right_arm']){
   const a=posture?.arms?.[side], js=Array.from({length:7},(_,i)=>joints.find(j=>j.name===side+'_joint'+(i+1)));
   if(paused||!a?.valid||!a.raised||a.moving||js.some(j=>!j?.fresh||!Number.isFinite(j.position)||!j.limits||j.position<j.limits[0]||j.position>j.limits[1])){delete this.candidates[side];continue}
   if(this.targets[side])continue;
   const c=this.candidates[side];
   if(!c||js.some((j,i)=>Math.abs(j.position-c.positions[i])>Math.PI/360)){this.candidates[side]={start:now,positions:js.map(j=>j.position),samples:1};continue}
   c.samples++;
   if(now-c.start>=1200&&c.samples>=3)this.targets[side]={side,time:new Date(now).toLocaleTimeString(),source:'auto',joints:js.map(j=>({name:j.name,position:j.position}))};
  }
  return this.targets;
 }
}
root.AutoRaiseTracker=AutoRaiseTracker;
root.GesturePlan=plan;if(typeof module!=='undefined'){module.exports=plan;module.exports.AutoRaiseTracker=AutoRaiseTracker;}
})(typeof window==='undefined'?globalThis:window);
