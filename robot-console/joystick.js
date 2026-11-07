(function(root){function vector(x,y,maxV,maxW){
 if(![x,y,maxV,maxW].every(Number.isFinite)||maxV<.01||maxV>.2||maxW<.01||maxW>.3)throw Error('操纵杆参数无效');
 const length=Math.hypot(x,y);if(length<.1)return {x:0,y:0,v:0,w:0};if(length>1){x/=length;y/=length}
 return {x,y,v:-y*maxV,w:-x*maxW};
}function blocked(s){if(!s.connected)return '不可操作：机器人连接未就绪，请刷新连接状态';if(s.fault)return '底盘故障：'+s.fault+'；已禁止移动，请核查现场状态';if(!s.available)return '不可操作：底盘反馈不可用';if(!s.confirmed)return '请先勾选下方的底盘现场安全确认';if(s.busy)return '请等待当前关节动作或预设结束';return ''}root.JoystickBlockedReason=blocked;root.JoystickVector=vector;if(typeof module!=='undefined'){module.exports=vector;module.exports.blocked=blocked;}})(typeof window==='undefined'?globalThis:window);
