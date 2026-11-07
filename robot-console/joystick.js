(function(root){function vector(x,y,maxV,maxW){
 if(![x,y,maxV,maxW].every(Number.isFinite)||maxV<.01||maxV>.2||maxW<.01||maxW>.3)throw Error('操纵杆参数无效');
 const length=Math.hypot(x,y);if(length<.1)return {x:0,y:0,v:0,w:0};if(length>1){x/=length;y/=length}
 return {x,y,v:-y*maxV,w:-x*maxW};
}root.JoystickVector=vector;if(typeof module!=='undefined')module.exports=vector;})(typeof window==='undefined'?globalThis:window);
