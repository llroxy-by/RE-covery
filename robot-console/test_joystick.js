const assert=require('node:assert/strict'),v=require('./joystick');
assert.equal(v(0,-1,.1,.2).v,.1);assert.equal(v(0,1,.1,.2).v,-.1);
assert.equal(v(-1,0,.1,.2).w,.2);assert.equal(v(1,0,.1,.2).w,-.2);
assert.deepEqual(v(.02,.02,.1,.2),{x:0,y:0,v:0,w:0});
const diagonal=v(5,-5,.1,.2);assert(Math.hypot(diagonal.x,diagonal.y)<=1.00001);assert(Math.abs(diagonal.v)<=.1);
assert.throws(()=>v(NaN,0,.1,.2));assert.throws(()=>v(0,1,.21,.2));console.log('Joystick mapping checks passed');

const ok={connected:true,available:true,confirmed:true,busy:false};assert.equal(v.blocked(ok),'');assert.equal(v.blocked({...ok,fault:'ControlStatus.FAULT'}),'');assert.match(v.blocked({...ok,confirmed:false}),/确认/);assert.match(v.blocked({...ok,connected:false}),/连接/);console.log('Joystick guard checks passed');
